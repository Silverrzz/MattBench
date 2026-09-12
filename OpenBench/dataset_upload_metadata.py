from OpenBench.dataset_manifest import analysis_files, validate_manifest


def upload_metadata(manifest, entry):
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
    statistics = manifest.get('statistics', {}) if analysis_files(files) == analysis_files(manifest['files']) else {}
    manifest = validate_manifest({**manifest, 'files': files, 'statistics': statistics})
    return manifest, {'analysis.txt': ''}
