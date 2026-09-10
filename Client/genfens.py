# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                           #
#   OpenBench is a chess engine testing framework by Andrew Grant.          #
#   <https://github.com/AndyGrant/OpenBench>  <andrew@grantnet.us>          #
#                                                                           #
#   OpenBench is free software: you can redistribute it and/or modify       #
#   it under the terms of the GNU General Public License as published by    #
#   the Free Software Foundation, either version 3 of the License, or       #
#   (at your option) any later version.                                     #
#                                                                           #
#   OpenBench is distributed in the hope that it will be useful,            #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#   GNU General Public License for more details.                            #
#                                                                           #
#   You should have received a copy of the GNU General Public License       #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.   #
#                                                                           #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

# The main purpose of this module is to invoke create_genfens_opening_book().
# Refer to Client/worker.py, or Scripts/genfens_engine.py for the arguments.
#
# We will execute engines with commands like the following:
#   ./engine "genfens N seed S book <None|Books/book.epd> <?extra>" "quit"
#
# This work is split over many engines. If a workload requires 1024 openings,
# and there are 16 threads, then each thread will generate 64 openings.
#
# create_genfens_opening_book() may raise utils.OpenBenchFailedGenfensException.

import collections
import concurrent.futures
import math
import os
import queue
import subprocess
import time
import threading

## Local imports must only use "import x", never "from x import ..."

import utils

def genfens_required_openings_each(config):

    runner_cnt  = config.workload['distribution']['runner-count']
    rounds_per  = config.workload['distribution']['rounds-per-runner']
    repeat      = config.workload['test']['play_reverses']
    total_games = runner_cnt * rounds_per // (1 + repeat)

    return math.ceil(total_games / config.threads)

def genfens_book_input_name(config):

    book_name = config.workload['test']['book']['name']
    book_none = book_name.upper() == 'NONE'

    return 'None' if book_none else os.path.join('Books', book_name)

def genfens_command_builder(args, index):

    command = ['./%s' % (args['engine'])]

    fstr = 'genfens %d seed %d book %s %s'
    command += [fstr % (args['N'], args['seeds'][index], args['book'], args['extra']), 'quit']

    return command

def genfens_single_threaded(command, output, index, expected, active, lock, stop):

    process = None
    count = 0
    logs = collections.deque(maxlen=8)

    try:
        with lock:
            if stop.is_set():
                return
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            active[index] = [process, time.monotonic()]

        for raw_line in iter(process.stdout.readline, b''):
            line = raw_line.decode('utf-8', errors='replace').rstrip()
            if line.startswith('info string genfens '):
                count += 1
                if count > expected:
                    raise ValueError('Generated more than %d openings' % expected)
                with lock:
                    active[index][1] = time.monotonic()
                output.put(('fen', index, line.split('genfens ', 1)[1]))
            else:
                logs.append(line[-1000:])

        returncode = process.wait()
        if not stop.is_set():
            if returncode or count != expected:
                raise RuntimeError('Exit code %d; generated %d/%d openings; output: %s'
                                   % (returncode, count, expected, ' | '.join(logs)))
            output.put(('done', index, None))

    except Exception as error:
        if not stop.is_set():
            output.put(('error', index, str(error)))

    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
        with lock:
            active.pop(index, None)

def genfens_progress_bar(curr, total):

    prev_progress = 50 * (curr - 1) // total
    curr_progress = 50 * (curr - 0) // total

    if curr_progress != prev_progress:
        bar_text = '=' * curr_progress + ' ' * (50 - curr_progress)
        print ('\r[%s] %d/%d' % (bar_text, curr, total), end='', flush=True)

def convert_fen_to_epd(fen):

    # Input  : rnbqkbnr/pppp2pp/4pp2/8/2P2P2/P7/1P1PP1PP/RNBQKBNR b KQkq - 0 3
    # Output : rnbqkbnr/pppp2pp/4pp2/8/2P2P2/P7/1P1PP1PP/RNBQKBNR b KQkq - hmvc 0; fmvn 3;

    halfmove, fullmove = fen.split()[4:]

    return ' '.join(fen.split()[:4]) + ' hmvc %d; fmvn %d;' % (int(halfmove), int(fullmove))

def create_genfens_opening_book(args):

    N          = args['N']
    threads    = args['threads']
    start_time = time.monotonic()
    output     = queue.Queue()
    active     = {}
    lock       = threading.Lock()
    stop       = threading.Event()
    executor   = None

    try:
        concurrency = min(threads, int(os.environ.get('OPENBENCH_GENFENS_CONCURRENCY', '32')))
        timeout = float(os.environ.get('OPENBENCH_GENFENS_TIMEOUT', '120'))
        if N < 1 or concurrency < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Opening count, concurrency and timeout must be positive')
        if len(args['seeds']) < threads:
            raise ValueError('Not enough genfens seeds for %d tasks' % threads)

        print('\nGenerating %d Openings using %d Concurrent Engines (%d Seed Tasks)...'
              % (N * threads, concurrency, threads))

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency)
        for index in range(threads):
            executor.submit(genfens_single_threaded, genfens_command_builder(args, index),
                            output, index, N, active, lock, stop)

        completed = generated = 0
        while completed < threads:
            try:
                kind, index, value = output.get(timeout=1)
            except queue.Empty:
                kind = None

            if kind == 'error':
                raise RuntimeError('Seed task %d (seed %s): %s' % (index, args['seeds'][index], value))
            if kind == 'done':
                completed += 1
            if kind == 'fen':
                args['output'].write(convert_fen_to_epd(value) + '\n')
                generated += 1
                genfens_progress_bar(generated, N * threads)

            with lock:
                stalled = [(index, process.pid) for index, (process, last_output) in active.items()
                           if time.monotonic() - last_output > timeout]
            if stalled:
                raise RuntimeError('No opening or exit for %.1fs from seed task/PID %s; generated %d/%d'
                                   % (timeout, stalled[:8], generated, N * threads))

        if generated != N * threads:
            raise RuntimeError('Generated %d/%d openings' % (generated, N * threads))

    except Exception as error:
        raise utils.OpenBenchFailedGenfensException('[%s] Genfens failed: %s' % (args['engine'], error)) from error

    finally:
        stop.set()
        with lock:
            for process, last_output in active.values():
                if process.poll() is None:
                    process.kill()
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    print('\nFinished Building Opening Book in %.3f seconds' % (time.monotonic() - start_time))
