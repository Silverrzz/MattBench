import bench
import utils


def run(config, branch, binary, expected, report):
    target = config.workload['test'][branch]
    automatic = target.get('automatic_bench', False)
    speed, nodes = bench.run_benchmark(binary, config.threads, 2 if automatic and not expected else 1, expected)
    if automatic:
        if nodes <= 0:
            raise utils.OpenBenchBadBenchException('The discovered bench must be positive.')
        response = report(config, 'clientWorkflowBench', {
            'test_id': config.workload['test']['id'], 'branch': branch,
            'engine_id': target['id'], 'sha': target['sha'], 'network': target['network'], 'bench': nodes,
        })
        response.raise_for_status()
        pinned = response.json()['bench']
        if nodes != pinned:
            raise utils.OpenBenchBadBenchException('[%s] Wrong Bench: %d (expected %d)' % (target['name'], nodes, pinned))
    return speed, nodes
