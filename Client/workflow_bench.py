import bench
import utils


def run(config, branch, binary, expected, report):
    target = config.workload['test'][branch]
    automatic = target.get('automatic_bench', False)
    options = target.get('bench_options')
    arguments = (binary, config.threads, 2 if automatic and not expected else 1, expected)
    speed, nodes = bench.run_benchmark(*arguments, options=options) if options is not None else bench.run_benchmark(*arguments)
    if automatic:
        if nodes <= 0:
            raise utils.OpenBenchBadBenchException('The discovered bench must be positive.')
        response = report(config, 'clientWorkflowBench', {
            'test_id': config.workload['test']['id'], 'branch': branch,
            'engine_id': target['id'], 'sha': target['sha'], 'network': target['network'], 'bench': nodes,
            **({'options': options} if options is not None else {}),
        })
        response.raise_for_status()
        pinned = response.json()['bench']
        if nodes != pinned:
            raise utils.OpenBenchBadBenchException('[%s] Wrong Bench: %d (expected %d)' % (target['name'], nodes, pinned))
    return speed, nodes
