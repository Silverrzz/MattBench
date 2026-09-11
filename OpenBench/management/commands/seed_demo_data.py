import bz2
import gzip
import hashlib
import io
import json
import math
import secrets
import sqlite3
import tarfile
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from OpenBench.models import DatasetUpload, Engine, EngineConfig, LLRHistory, Machine, Network
from OpenBench.models import Profile, Result, SPSAParameter, SPSARun, Test
from OpenBench.models import TrainingArtifact, TrainingCheckpoint, TrainingRun, TrainingSchedule, TrainingWorker, WorkloadPreset
from OpenBench.training import DEFAULT_SETTINGS, validate_schedule


MARKER = 'mattbench-ui-v1'


def stable_id(key):
    return uuid.uuid5(uuid.NAMESPACE_URL, MARKER + '/' + key)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def write_once(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise CommandError('Demo file conflicts with an existing file: %s' % path)
    else:
        path.write_bytes(content)


class Command(BaseCommand):
    help = 'Add local demo workloads, schedules, networks and workers without dispatching jobs.'

    def add_arguments(self, parser):
        parser.add_argument('--owner', default='devadmin')

    def handle(self, *args, **options):
        user_model = get_user_model()
        owner = user_model.objects.filter(username=options['owner']).first()
        if owner is None:
            raise CommandError('Create the specified owner before adding demo data.')
        engines = list(EngineConfig.objects.filter(enabled=True, name__in=['Heimdall', 'Prelude', 'Stormphrax', 'Tarnished']).order_by('name'))
        if len(engines) < 4:
            raise CommandError('The four demo engine configurations must be available.')
        database = settings.DATABASES['default']
        if database['ENGINE'].endswith('sqlite3'):
            backup = Path(settings.BASE_DIR) / 'venv' / 'before-demo-ui.sqlite3'
            if not backup.exists():
                with sqlite3.connect(str(database['NAME'])) as source, sqlite3.connect(str(backup)) as destination:
                    source.backup(destination)
        with transaction.atomic():
            self.seed(owner, engines)
        self.stdout.write(self.style.SUCCESS('Demo data ready: 16 tests, 8 datagens, 8 tunes, 12 training runs, 12 schedules, 12 networks, 9 workers and 12 presets.'))

    def seed(self, owner, engines):
        now = timezone.now()
        authors = [owner]
        for name in ('demo_alex', 'demo_mira', 'demo_sam'):
            author, created = get_user_model().objects.get_or_create(username=name, defaults={'is_active': False})
            if created:
                author.set_unusable_password()
                author.save(update_fields=['password'])
                Profile.objects.create(user=author, enabled=True)
            authors.append(author)

        networks = {}
        for engine in engines:
            networks[engine.pk] = []
            for index in range(3):
                name = 'demo-%s-sb%03d' % (engine.name.lower(), (index + 1) * 100)
                content = ('MAT TBENCH DEMO NETWORK: %s\n' % name).encode() + bytes(range(256)) * 64
                sha = hashlib.sha256(content).hexdigest()[:8].upper()
                existing = Network.objects.filter(sha256=sha).exclude(name=name).exists()
                if existing:
                    raise CommandError('A demo network hash conflicts with an existing network.')
                write_once(Path(settings.MEDIA_ROOT) / sha, content)
                network, _ = Network.objects.get_or_create(sha256=sha, name=name, engine=engine.name, defaults={'author': owner.username})
                networks[engine.pk].append(network)

        machines = []
        hardware = [
            ('cedar-7950x', 'AMD Ryzen 9 7950X', 32, 64),
            ('birch-epyc', 'AMD EPYC 9654', 128, 256),
            ('maple-14900k', 'Intel Core i9-14900K', 32, 64),
            ('aspen-5950x', 'AMD Ryzen 9 5950X', 32, 32),
            ('elm-7900', 'AMD Ryzen 9 7900', 24, 32),
        ]
        for index, (name, cpu, threads, ram) in enumerate(hardware):
            machine, _ = Machine.objects.get_or_create(info__demo=MARKER, info__machine_name=name, defaults={
                'user': authors[index % len(authors)], 'secret': secrets.token_hex(32),
                'info': {'demo': MARKER, 'demo_online': index < 4, 'machine_name': name, 'cpu_name': cpu,
                         'concurrency': threads, 'physical_cores': threads // 2, 'logical_cores': threads,
                         'ram_total_mb': ram * 1024, 'os_name': 'Linux' if index != 2 else 'Windows',
                         'isa_name': 'AVX2', 'python_ver': '3.11.9', 'client_ver': 46, 'syzygy_max': 6,
                         'supported': [engine.name for engine in engines], 'noisy': False},
                'mnps': threads * 1.4, 'dev_mnps': threads * .72, 'base_mnps': threads * .68,
            })
            Machine.objects.filter(pk=machine.pk).update(updated=now - timedelta(minutes=0 if index < 4 else 180))
            machines.append(machine)

        titles = [
            'history-gravity', 'see-pruning', 'lmr-adjustment', 'correction-history',
            'aspiration-window', 'singular-extension', 'nnue-sb300', 'quiet-history',
            'razoring-margin', 'probcut-tuning', 'continuation-history', 'time-management',
            'futility-depth', 'threat-buckets', 'pawn-correction', 'late-move-pruning',
        ]
        workloads = []
        for index in range(32):
            engine = engines[index % len(engines)]
            mode = 'SPRT' if index < 16 else 'DATAGEN' if index < 24 else 'SPSA'
            local_index = index if index < 16 else (index - 16) % 8
            completed = local_index >= (6 if mode == 'SPRT' else 3)
            approved = local_index != (5 if mode == 'SPRT' else 2)
            title = titles[index] if mode == 'SPRT' else ('selfplay-%dk-part-%02d' % (5 + local_index * 2, local_index + 1) if mode == 'DATAGEN' else 'search-parameters-round-%02d' % (local_index + 1))
            name = 'demo-' + title
            source = engine.settings['source']
            dev, _ = Engine.objects.get_or_create(name=name, sha=digest(name)[:40], source=source, defaults={'bench': 1240000 + index * 4171})
            base, _ = Engine.objects.get_or_create(name='demo-master-' + engine.name.lower(), sha=digest(engine.name)[:40], source=source, defaults={'bench': 1328045})
            factor = 16 + local_index * 7
            buckets = [120 * factor, 350 * factor, 760 * factor, (350 + local_index % 3) * factor, 122 * factor]
            if mode == 'DATAGEN':
                buckets = [value * 8 for value in buckets]
            games = 2 * sum(buckets) if approved else 0
            buckets = buckets if approved else [0] * 5
            wins = buckets[3] + 2 * buckets[4]
            losses = 2 * buckets[0] + buckets[1]
            net = networks[engine.pk][local_index % 3]
            execution = {'demo': MARKER, 'demo_key': str(index), 'variant': 'standard', 'fastchess_variant': 'standard',
                         'syzygy': True, 'runner': {'repo_url': 'https://github.com/AndyGrant/fastchess', 'repo_ref': 'master', 'min_version': '1.8.1'}}
            values = dict(author=authors[index % 4].username, info='Demo workload', dev=dev, base=base,
                          dev_repo=source, base_repo=source, dev_engine=engine.name, base_engine=engine.name,
                          dev_options='Threads=1 Hash=16', base_options='Threads=1 Hash=16',
                          dev_network=net.sha256, dev_netname=net.name, base_network=net.sha256, base_netname=net.name,
                          dev_time_control='N=5000' if mode == 'DATAGEN' else '8.0+0.08',
                          base_time_control='N=5000' if mode == 'DATAGEN' else '8.0+0.08',
                          book_name='NONE' if mode == 'DATAGEN' else '2moves_v1.epd', test_mode=mode,
                          elolower=0, eloupper=3, alpha=.05, beta=.05, lowerllr=-2.944, upperllr=2.944,
                          currentllr=(-3.04 if local_index % 3 == 0 else 3.08) if completed else .25 + local_index * .28,
                          max_games=games if completed and mode == 'DATAGEN' else 4000000 if mode == 'DATAGEN' else 128000,
                          games=games, wins=wins, losses=losses, draws=games - wins - losses,
                          **dict(zip(('LL', 'LD', 'DD', 'DW', 'WW'), buckets)),
                          approved=approved, finished=completed, passed=completed and mode == 'SPRT' and local_index % 3 != 0,
                          failed=completed and mode == 'SPRT' and local_index % 3 == 0,
                          execution=execution, throughput=100, scale_nps=engine.settings['nps'],
                          upload_pgns='COMPACT' if mode == 'DATAGEN' else 'FALSE')
            workload, _ = Test.objects.get_or_create(execution__demo=MARKER, execution__demo_key=str(index), defaults=values)
            Test.objects.filter(pk=workload.pk).update(creation=now - timedelta(hours=4 + index * 9), updated=now - timedelta(minutes=index * 11 if completed else 0))
            workloads.append(workload)
            Result.objects.get_or_create(test=workload, machine=machines[index % len(machines)], defaults={
                **{field: values[field] for field in ('games', 'wins', 'losses', 'draws', 'LL', 'LD', 'DD', 'DW', 'WW')},
                'dev_nodes': games * 270000, 'base_nodes': games * 260000,
                'dev_time': games * 240, 'base_time': games * 240, 'dev_time_scaled': games * 240, 'base_time_scaled': games * 240,
            })
            if mode == 'SPRT' and games:
                for point in range(1, 33):
                    LLRHistory.objects.get_or_create(test=workload, games=games * point // 32, defaults={'llr': values['currentllr'] * point / 32 + .16 * math.sin(point / 2)})
            if mode == 'SPSA':
                tuning, _ = SPSARun.objects.get_or_create(tune=workload, defaults={'reporting_type': 'BULK', 'distribution_type': 'MULTIPLE',
                    'alpha': .602, 'gamma': .101, 'iterations': 2000, 'pairs_per': 32, 'a_ratio': .1})
                for p, pname in enumerate(('LMRBase', 'LMRDivisor', 'FutilityMargin', 'NullMoveBase', 'ProbCutMargin', 'HistoryBonus')):
                    start = 80 + p * 45
                    SPSAParameter.objects.get_or_create(spsa_run=tuning, index=p, defaults={'name': pname, 'value': start + 4.2 - p,
                        'is_float': False, 'start': start, 'min_value': start * .5, 'max_value': start * 1.5,
                        'c_end': 2, 'r_end': .002, 'c_value': 4.3, 'a_value': 18})
            if mode == 'DATAGEN':
                if completed:
                    pgn = ('[Event "MattBench demo"]\n[White "%s"]\n[Black "%s"]\n[Result "1/2-1/2"]\n\n1. e4 e5 2. Nf3 Nc6 1/2-1/2\n' % (name, name)).encode()
                    compressed = bz2.compress(pgn)
                    buffer = io.BytesIO()
                    with tarfile.open(fileobj=buffer, mode='w') as archive:
                        member = tarfile.TarInfo('demo.pgn.bz2')
                        member.size = len(compressed)
                        archive.addfile(member, io.BytesIO(compressed))
                    write_once(Path(settings.MEDIA_ROOT) / 'PGNs' / ('%d.pgn.tar' % workload.pk), buffer.getvalue())
                state = 'COMPLETED' if completed else 'UPLOADING' if local_index == 1 else 'QUEUED'
                DatasetUpload.objects.get_or_create(workload=workload, owner=owner, repo='mattbench-demo/' + engine.name.lower(), filename='%s.pgn.tar' % name,
                    defaults={'state': state, 'progress': 100 if completed else 43 if local_index == 1 else 0,
                              'stage': 'Published' if completed else 'Uploading archive', 'revision': digest(name)[:40] if completed else ''})
        for machine, workload in zip(machines[:3], (workloads[0], workloads[16], workloads[24])):
            Machine.objects.filter(pk=machine.pk).update(workload=workload.pk)

        schedules = []
        schedule_names = ('Baseline 768', 'Wide 1536', 'Fine-tune 768')
        for engine in engines:
            for index, name in enumerate(schedule_names):
                config = {**DEFAULT_SETTINGS, 'min_vram_gb': 16 if index == 1 else 8, 'threads': 8, 'resume_supported': True}
                files = {'examples/mattbench.rs': 'fn main() {\n    eprintln!("Demo schedule: replace with your Bullet training code.");\n    std::process::exit(1);\n}\n',
                         'network.json': json.dumps({'architecture': [768 if index != 1 else 1536, 16, 32, 1], 'superbatches': 600, 'batch_size': 16384, 'learning_rate': .001 if index != 2 else .0001}, indent=2),
                         'mattbench-demo.json': json.dumps({'demo': MARKER})}
                validate_schedule(files, config)
                schedule = TrainingSchedule(engine=engine, owner=owner if index == 2 else None, name='Demo ' + name, files=files, settings=config, version=2 + index)
                schedules.append(schedule)
            for kind, title in (('TEST', 'Demo STC'), ('TUNE', 'Demo search tune'), ('DATAGEN', 'Demo 5k selfplay')):
                preset = WorkloadPreset.objects.filter(engine=engine, workload_type=kind).first()
                WorkloadPreset.objects.get_or_create(id=stable_id('preset/' + engine.name + '/' + kind),
                    defaults={'engine': engine, 'owner': owner, 'name': title, 'workload_type': kind, 'position': 100, 'settings': preset.settings if preset else {}})

        workers = []
        for index, (name, gpu, vram) in enumerate((('willow-5090', 'NVIDIA GeForce RTX 5090', 32), ('oak-4090', 'NVIDIA GeForce RTX 4090', 24), ('pine-3090', 'NVIDIA GeForce RTX 3090', 24), ('alder-7900xtx', 'AMD Radeon RX 7900 XTX', 24))):
            worker, _ = TrainingWorker.objects.get_or_create(id=stable_id('worker/' + name), defaults={'owner': owner, 'name': name, 'secret_hash': digest(secrets.token_hex(32)),
                'updated': now - timedelta(minutes=0 if index < 3 else 240), 'info': {'demo': MARKER, 'demo_online': index < 3,
                'gpu': gpu, 'backend': 'cuda' if index < 3 else 'rocm', 'vram_gb': vram, 'ram_gb': 64, 'threads': 16, 'disk_gb': 840, 'device': 0, 'protocol': 2}})
            workers.append(worker)

        states = ('TRAINING', 'DOWNLOADING', 'COMPILING', 'QUEUED', 'QUEUED', 'VALIDATING', 'COMPLETED', 'COMPLETED', 'FAILED', 'COMPLETED', 'CANCELLED', 'COMPLETED')
        for index, (schedule, state) in enumerate(zip(schedules, states)):
            terminal = state in ('COMPLETED', 'FAILED', 'CANCELLED')
            sb = 600 if state == 'COMPLETED' else 284 if state in ('TRAINING', 'FAILED', 'CANCELLED') else 0
            metrics = {'progress': 100 if state == 'COMPLETED' else 47.3 if sb else 36 if state == 'DOWNLOADING' else 0}
            history = []
            if sb:
                metrics.update(loss=.08172 + index * .0002, superbatch=sb, positions_per_second=7200000 if state == 'TRAINING' else 5600000,
                               elapsed_seconds=sb * 24, gpu_percent=98, gpu_used_mb=19800, cpu_percent=46, ram_used_gb=18.4, disk_free_gb=738, learning_rate=.00035)
                history = [{'superbatch': point * sb // 60, 'loss': .08172 + .06 * math.exp(-point / 12) + .0003 * math.sin(point), 'time': point * 120} for point in range(61)]
            if state == 'DOWNLOADING':
                metrics['downloaded_bytes'] = 13690208256
            run, _ = TrainingRun.objects.get_or_create(snapshot__demo=MARKER, snapshot__demo_key=str(index), defaults={
                'owner': owner, 'engine': schedule.engine, 'name': 'demo-%s-%s' % (schedule.engine.name.lower(), ('baseline', 'wide', 'finetune')[index % 3]),
                'snapshot': {'demo': MARKER, 'demo_key': str(index), 'name': schedule.name, 'version': schedule.version, 'settings': schedule.settings, 'files': schedule.files, 'bullet_commit': digest('bullet-demo')[:40]},
                'dataset': {'repo': 'mattbench-demo/' + schedule.engine.name.lower(), 'revision': 'main', 'commit': digest('dataset-demo')[:40], 'files': [{'path': 'selfplay.pgn.tar', 'size': 38000000000}]},
                'parameters': {'hidden': 1536 if index % 3 == 1 else 768, 'superbatches': 600},
                'worker': workers[index] if index < 3 else workers[index % 3] if terminal else None,
                'state': state, 'metrics': metrics, 'history': history,
                'created': now - timedelta(hours=1 + index * 6), 'started': now - timedelta(hours=1 + index * 6) if sb or index < 3 else None,
                'finished': now - timedelta(hours=index - 5) if terminal else None, 'updated': now,
                'log_tail': '\n'.join(['Demo training output', 'Dataset: shuffled viriformat', 'Build completed'] + ['Superbatch %d | loss %.6f | 7.2M positions/s' % (point['superbatch'], point['loss']) for point in history[-24:]]),
                'error': 'Worker heartbeat lost. Checkpoint SB 280 is available.' if state == 'FAILED' else '',
            })
            write_once(Path(settings.TRAINING_ROOT) / str(run.pk) / 'worker.log', run.log_tail.encode())
            if state == 'COMPLETED':
                net = networks[schedule.engine_id][2]
                content = (Path(settings.MEDIA_ROOT) / net.sha256).read_bytes()
                path = 'demo/%d/final-network.bin' % run.pk
                write_once(Path(settings.TRAINING_ROOT) / path, content)
                TrainingArtifact.objects.get_or_create(run=run, name='final-network.bin', defaults={'kind': 'network', 'path': path,
                    'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'network': net})
            if sb:
                for cp_index, superbatch in enumerate((100, 200, 280 if sb < 600 else 600)):
                    prefix = 'demo/%s/sb-%d' % (run.pk, superbatch)
                    archive_name = 'checkpoint-%d.tar.gz' % superbatch
                    content = ('Demo checkpoint SB %d' % superbatch).encode()
                    buffer = io.BytesIO()
                    with tarfile.open(fileobj=buffer, mode='w') as archive:
                        member = tarfile.TarInfo('demo-state.json')
                        member.size = len(content)
                        archive.addfile(member, io.BytesIO(content))
                    archive_bytes = gzip.compress(buffer.getvalue(), mtime=0)
                    net = networks[schedule.engine_id][cp_index]
                    net_bytes = (Path(settings.MEDIA_ROOT) / net.sha256).read_bytes()
                    artifacts = []
                    for name, kind, data in ((archive_name, 'checkpoint', archive_bytes), ('network-%d.bin' % superbatch, 'network', net_bytes)):
                        path = prefix + '/' + name
                        write_once(Path(settings.TRAINING_ROOT) / path, data)
                        artifact, _ = TrainingArtifact.objects.get_or_create(run=run, name=name, defaults={'kind': kind, 'path': path, 'size': len(data),
                            'sha256': hashlib.sha256(data).hexdigest(), 'network': net if kind == 'network' and cp_index == 2 and state == 'COMPLETED' else None})
                        artifacts.append(artifact)
                    TrainingCheckpoint.objects.get_or_create(run=run, superbatch=superbatch, defaults={'archive': artifacts[0], 'network': artifacts[1], 'metadata': {'demo': MARKER}})
