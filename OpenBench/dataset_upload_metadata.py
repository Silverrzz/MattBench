import bz2
import hashlib
import io
import json
import re
import tarfile
import time

from django.core.exceptions import ValidationError

from OpenBench.dataset_manifest import analysis_files, validate_manifest


def analyse_archive(source, progress):
    results = {'1-0': 0, '0-1': 0, '1/2-1/2': 0, '*': 0}
    members = 0
    header = re.compile(r'^\[Result\s+"(1-0|0-1|1/2-1/2|\*)"\s*\]$')
    last_progress = time.monotonic()
    size = source.stat().st_size
    with tarfile.open(source, mode='r:') as archive:
        for member in archive:
            if not member.isfile():
                continue
            if not member.name.lower().endswith(('.pgn', '.pgn.bz2')):
                raise ValidationError('Unsupported PGN archive member: ' + member.name)
            with archive.extractfile(member) as raw:
                stream = bz2.BZ2File(raw) if member.name.lower().endswith('.bz2') else raw
                with io.TextIOWrapper(stream, encoding='utf-8') as text:
                    for line in text:
                        match = header.fullmatch(line.strip())
                        if match:
                            results[match.group(1)] += 1
                        if time.monotonic() - last_progress >= 2:
                            progress(min(100, 100 * archive.fileobj.tell() / max(1, size)))
                            last_progress = time.monotonic()
            members += 1
            if time.monotonic() - last_progress >= 2:
                progress(min(100, 100 * archive.fileobj.tell() / max(1, size)))
                last_progress = time.monotonic()
    progress(100)
    games = sum(results.values())
    if not games:
        raise ValidationError('The PGN archive contains no games with valid result headers.')
    return {'analysis_kind': 'pgn_headers', 'games': games, 'pgn_files': members, 'results': results,
            'analysis': 'PGN archive header analysis\nGames: %d\nPGN files: %d\nWhite wins: %d\nBlack wins: %d\nDraws: %d\nUnfinished: %d\nPosition analysis is performed during dataset preparation.' % (
                games, members, results['1-0'], results['0-1'], results['1/2-1/2'], results['*'])}


def upload_metadata(manifest, entry, statistics, repo, revision, workload):
    files = []
    represented = False
    for file in manifest['files']:
        if file['path'] == entry['path']:
            files.append({**file, **entry})
            represented = True
        else:
            files.append(file)
            represented = represented or entry['path'] in file.get('sources', [])
    if not represented:
        files.append(entry)
    report = 'analysis/uploads/%s.txt' % hashlib.sha256((entry['path'] + '\0' + entry['sha256']).encode('utf-8')).hexdigest()
    inputs = analysis_files([entry])
    analyses = [analysis for analysis in manifest.get('analyses', [])
                if not (analysis.get('statistics', {}).get('analysis_kind') == 'pgn_headers'
                        and analysis_files(analysis['files']) == inputs)]
    reports = {}
    previous_inputs = analysis_files(manifest['files'])
    previous_statistics = manifest.get('statistics', {})
    if previous_statistics.get('analysis') and not any(analysis_files(analysis['files']) == previous_inputs and analysis['statistics'] == previous_statistics for analysis in analyses):
        previous_report = 'analysis/uploads/previous-%s.txt' % hashlib.sha256(json.dumps(
            {'files': previous_inputs, 'statistics': previous_statistics}, sort_keys=True).encode('utf-8')).hexdigest()
        analyses.append({'files': previous_inputs, 'statistics': previous_statistics,
                         'report': previous_report, 'source_revision': revision})
        reports[previous_report] = 'Previous dataset analysis\nFiles: %s\n\n%s\n' % (
            ', '.join(file['path'] for file in previous_inputs), previous_statistics['analysis'])
    analyses.insert(0, {'files': inputs, 'statistics': statistics, 'report': report,
                     'source_revision': revision, 'workload': workload})
    overall = manifest.get('statistics', {}) if analysis_files(files) == analysis_files(manifest['files']) else {}
    if analysis_files(files) == inputs and (not overall.get('analysis') or overall.get('analysis_kind') == 'pgn_headers'):
        overall = statistics
    covered = []
    for file in files:
        matches = [analysis['statistics'] for analysis in analyses
                   if analysis['statistics'].get('analysis_kind') == 'pgn_headers'
                   and analysis_files(analysis['files']) == analysis_files([file])]
        if matches:
            covered.append(matches[0])
    if len(covered) == len(files) and (not overall.get('analysis') or overall.get('analysis_kind') == 'pgn_headers'):
        results = {result: sum(item['results'][result] for item in covered) for result in ('1-0', '0-1', '1/2-1/2', '*')}
        overall = {'analysis_kind': 'pgn_headers', 'games': sum(results.values()),
                   'pgn_files': sum(item['pgn_files'] for item in covered), 'results': results,
                   'source_bytes': sum(file['size'] for file in files), 'source_files': len(files)}
        overall['analysis'] = 'PGN header analysis across all %d dataset archives\nGames: %d\nWhite wins: %d\nBlack wins: %d\nDraws: %d\nUnfinished: %d\nPosition analysis is performed during dataset preparation.' % (
            len(files), overall['games'], results['1-0'], results['0-1'], results['1/2-1/2'], results['*'])
    manifest = validate_manifest({**manifest, 'files': files, 'statistics': overall, 'analyses': analyses})
    detail = 'Dataset archive analysis\nRepository: %s\nArchive: %s\nSHA256: %s\nWorkload: %d\n\n%s\n' % (
        repo, entry['path'], entry['sha256'], workload, statistics['analysis'])
    summary = 'Dataset analysis\nRepository: %s\nDataset files: %d\nDataset bytes: %d\n\nFiles:\n%s\n' % (
        repo, len(files), sum(file['size'] for file in files),
        '\n'.join('%s (%d bytes, %s) SHA256: %s' % (file['path'], file['size'], file['format'], file.get('sha256') or 'unavailable') for file in files))
    if overall:
        summary += '\nDataset statistics:\n%s\n' % json.dumps(overall, indent=2, sort_keys=True)
    if len(covered) != len(files):
        summary += '\nPGN header analysis coverage: %d of %d current dataset files. Per-archive counts below are not totals for the entire dataset.\n' % (len(covered), len(files))
    for analysis in analyses:
        summary += '\nAnalysis for: %s\nReport: %s\n%s\n' % (
            ', '.join(file['path'] for file in analysis['files']), analysis['report'], analysis['statistics']['analysis'])
    reports.update({report: detail, 'analysis.txt': summary})
    return manifest, reports
