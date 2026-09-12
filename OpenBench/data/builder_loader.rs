use std::{
    fs::File,
    io::{BufReader, Cursor},
    sync::mpsc::{self, SyncSender},
};

use bullet_lib::game::formats::bulletformat::ChessBoard;

use std::cell::{Cell, RefCell};

use bullet_trainer::reader::DataReader;
use bullet_lib::value::loader::viribinpack::{Filter, Game};
use rand::Rng;

#[derive(Clone)]
pub struct PositionFilter {
    pub base: Filter,
    pub max_ply: u32,
    pub min_eval: u32,
    pub max_pieces: u32,
    pub piece_count_keep: [f64; 33],
    pub target_distribution: bool,
}

#[derive(Clone)]
struct SamplingStats { counts: [u64; 33], total: u64 }
impl Default for SamplingStats {
    fn default() -> Self { Self { counts: [0; 33], total: 0 } }
}

impl SamplingStats {
    fn acceptance(&mut self, count: usize, target: f64) -> f64 {
        self.counts[count] += 1;
        self.total += 1;
        (0.5 * target / (self.counts[count] as f64 / self.total as f64)).clamp(0.0, 1.0)
    }
}

#[derive(Clone)]
pub struct ViriBinpackLoader {
    file_paths: Vec<String>,
    buffer_size: usize,
    threads: usize,
    filter: PositionFilter,
}

impl ViriBinpackLoader {
    pub fn new_concat_multiple(
        paths: &[&str],
        buffer_size_mb: usize,
        threads: usize,
        filter: PositionFilter,
    ) -> Self {
        assert!(filter.base.random_fen_skip_probability < 1.0, "Random FEN skip probability 1 discards every position; lower it to train");
        Self {
            file_paths: paths.iter().map(|x| x.to_string()).collect(),
            buffer_size: buffer_size_mb * 1024 * 1024 / std::mem::size_of::<ChessBoard>() / 2,
            threads,
            filter,
        }
    }
}

impl DataReader<ChessBoard> for ViriBinpackLoader {
    fn read_chunks<F: FnMut(&[ChessBoard]) -> bool>(&self, _: usize, mut f: F) {
        let mut shuffle_buffer = Vec::new();
        shuffle_buffer.reserve_exact(self.buffer_size);

        let file_paths = self.file_paths.clone();
        let buffer_size = self.buffer_size;
        let threads = self.threads;
        let filter = self.filter.clone();

        let (sender, receiver) = mpsc::sync_channel::<Vec<Vec<u8>>>(4);
        let (msg_sender, msg_receiver) = mpsc::sync_channel::<bool>(1);

        std::thread::spawn(move || {
            let mut games = Vec::new();

            'dataloading: loop {
                for file_path in &file_paths {
                    let mut reader = BufReader::new(File::open(file_path.as_str()).unwrap());

                    loop {
                        let mut buf = Vec::new();
                        if Game::deserialise_fast_into_buffer(&mut reader, &mut buf).is_err() {
                            break;
                        }

                        games.push(buf);

                        if games.len().is_multiple_of(8192 * threads) {
                            if msg_receiver.try_recv().unwrap_or(false) || sender.send(games).is_err() {
                                break 'dataloading;
                            }

                            games = Vec::new();
                        }
                    }
                }
                if !games.is_empty() && sender.send(std::mem::take(&mut games)).is_err() {
                    break;
                }
                if sender.send(Vec::new()).is_err() {
                    break;
                }
            }
        });

        let (game_sender, game_receiver) = mpsc::sync_channel::<Vec<ChessBoard>>(4 * self.threads);
        let (game_msg_sender, game_msg_receiver) = mpsc::sync_channel::<bool>(1);

        std::thread::spawn(move || {
            let mut kept_in_pass = 0;
            // One persistent counter set per conversion worker, reset only when
            // read_chunks starts again (including workload continuation).
            let mut stats = vec![SamplingStats::default(); threads];
            'dataloading: while let Ok(games) = receiver.recv() {
                if game_msg_receiver.try_recv().unwrap_or(false) {
                    msg_sender.send(true).unwrap();
                    break 'dataloading;
                }

                if games.is_empty() {
                    if kept_in_pass == 0 {
                        let _ = game_sender.send(Vec::new());
                        break;
                    }
                    kept_in_pass = 0;
                } else {
                    kept_in_pass += convert_buffer(threads, &game_sender, &games, &filter, &mut stats);
                }
            }
        });

        let (buffer_sender, buffer_receiver) = mpsc::sync_channel::<Vec<ChessBoard>>(0);
        let (buffer_msg_sender, buffer_msg_receiver) = mpsc::sync_channel::<bool>(1);

        std::thread::spawn(move || {
            'dataloading: while let Ok(game) = game_receiver.recv() {
                if game.is_empty() {
                    let _ = buffer_sender.send(Vec::new());
                    break;
                }
                if buffer_msg_receiver.try_recv().unwrap_or(false) {
                    game_msg_sender.send(true).unwrap();
                    break 'dataloading;
                }

                if shuffle_buffer.len() + game.len() < shuffle_buffer.capacity() {
                    shuffle_buffer.extend_from_slice(&game);
                } else {
                    let diff = shuffle_buffer.capacity() - shuffle_buffer.len();
                    if diff > 0 {
                        shuffle_buffer.extend_from_slice(&game[..diff]);
                    }

                    shuffle(&mut shuffle_buffer);

                    if buffer_msg_receiver.try_recv().unwrap_or(false) || buffer_sender.send(shuffle_buffer).is_err() {
                        game_msg_sender.send(true).unwrap();
                        break 'dataloading;
                    }

                    shuffle_buffer = Vec::new();
                    shuffle_buffer.reserve_exact(buffer_size);
                    shuffle_buffer.extend_from_slice(&game[diff..]);
                }
            }
        });

        'dataloading: while let Ok(shuffle_buffer) = buffer_receiver.recv() {
            assert!(!shuffle_buffer.is_empty(), "No positions survived a full dataset pass; loosen the position filters or sampling probabilities");
            if f(&shuffle_buffer) {
                buffer_msg_sender.send(true).unwrap();
                break 'dataloading;
            }
        }

        drop(buffer_receiver);
    }
}

fn convert_buffer(threads: usize, sender: &SyncSender<Vec<ChessBoard>>, games: &[Vec<u8>], filter: &PositionFilter, stats: &mut [SamplingStats]) -> usize {
    let chunk_size = games.len().div_ceil(threads);
    let kept = std::sync::atomic::AtomicUsize::new(0);

    std::thread::scope(|s| {
        for (chunk, stats) in games.chunks(chunk_size).zip(stats.iter_mut()) {
            let this_sender = sender.clone();
            let kept = &kept;
            s.spawn(move || {
                let mut buffer = Vec::new();

                let mut reusable = Vec::new();
                for game_bytes in chunk {
                    let game = Game::deserialise_from(&mut Cursor::new(game_bytes), reusable).unwrap();
                    parse_into_buffer(&game, &mut buffer, filter, stats);
                    reusable = game.moves;
                }

                kept.fetch_add(buffer.len(), std::sync::atomic::Ordering::Relaxed);
                if !buffer.is_empty() {
                    let _ = this_sender.send(buffer);
                }
            });
        }
    });
    kept.load(std::sync::atomic::Ordering::Relaxed)
}

fn parse_into_buffer(game: &Game, buffer: &mut Vec<ChessBoard>, filter: &PositionFilter, stats: &mut SamplingStats) {
    let clock = Cell::new(0u8);
    let stats = RefCell::new(stats);
    game.splat_to_bulletformat_with_filter_callback(
        |mut board| {
            board.extra[0] = clock.get();
            buffer.push(board);
            Ok(())
        },
        |mv, eval, board, wdl, rng| {
            clock.set(board.fifty_move_counter());
            let pieces = board.pieces.occupied().count();
            board.ply() > filter.max_ply as usize
                || eval.unsigned_abs() < filter.min_eval
                || pieces > filter.max_pieces
                || pieces > 32
                || filter.base.should_filter(mv, eval, board, wdl, rng)
                || !rng.random_bool(if filter.target_distribution {
                    stats.borrow_mut().acceptance(pieces as usize, filter.piece_count_keep[pieces as usize])
                } else { filter.piece_count_keep[pieces as usize] })
        },
    ).unwrap();
}

fn shuffle(data: &mut [ChessBoard]) {
    let seed = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_micros();
    let mut rng = oorandom::Rand64::new(seed);

    for i in (0..data.len()).rev() {
        let idx = rng.rand_range(0..i as u64 + 1) as usize;
        data.swap(idx, i);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use bullet_lib::game::formats::viriformat::chess::board::Board;

    #[test]
    fn counters_survive_conversion_chunks_per_worker() {
        let mut board = Board::new();
        board.set_startpos();
        let mut game = Game::new(&board);
        game.add_move(board.parse_uci("e2e4").unwrap(), 0);
        let mut bytes = Vec::new();
        game.serialise_into(&mut bytes).unwrap();
        let games = vec![bytes.clone(), bytes];
        let filter = PositionFilter { base: Filter::UNRESTRICTED, max_ply: 1000, min_eval: 0, max_pieces: 32,
            piece_count_keep: [1.0; 33], target_distribution: true };
        let (sender, _receiver) = mpsc::sync_channel(8);
        let mut stats = vec![SamplingStats::default(); 2];
        convert_buffer(2, &sender, &games, &filter, &mut stats);
        convert_buffer(2, &sender, &games, &filter, &mut stats);
        assert_eq!(stats.iter().map(|s| s.total).collect::<Vec<_>>(), vec![2, 2]);
        assert_eq!(stats[0].counts[32], 2);
        assert_eq!(SamplingStats::default().total, 0);
    }

    #[test]
    fn target_acceptance_uses_all_eligible_positions_and_clamps() {
        let mut stats = SamplingStats::default();
        assert_eq!(stats.acceptance(32, 0.0), 0.0);
        assert_eq!(stats.acceptance(2, 1.0), 1.0);
        assert!((stats.acceptance(32, 0.4) - 0.3).abs() < 1e-12);
        assert_eq!(stats.total, 3);
    }
}
