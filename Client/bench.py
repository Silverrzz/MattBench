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

# The sole purpose of this module is to invoke run_benchmark().
#
#   - binary   : Relative path to, and including, the Binary File
#   - threads  : Number of concurrent benches to run
#   - sets     : Number of times to repeat this experiment
#   - expected : None, or an expected value, which if not matched raises Exceptions
#
# run_benchmark() may raise utils.OpenBenchBadBenchException.
# An associated error message, including the binary name, is included

import multiprocessing
import os
import queue
import re
import shlex
import subprocess
import sys

## Local imports must only use "import x", never "from x import ..."

import utils

MAX_BENCH_TIME_SECONDS = 60

def parse_stream_output(stream):

    nps = bench = None # Search through output Stream
    for line in stream.decode('utf-8').strip().split('\n')[::-1]:

        # Convert non alpha-numerics to spaces
        line = re.sub(r'[^a-zA-Z0-9 ]+', ' ', line)

        # Multiple methods, including Ethereal and Stockfish
        nps_pattern   = r'(\d+\s+nps)|(nps\s+\d+)|(nodes second\s+\d+)'
        bench_pattern = r'(\d+\s+nodes)|(nodes\s+\d+)|(nodes searched\s+\d+)'

        # Search for and set only once the NPS and Bench values
        re_nps = re.search(r'\b(?:nps|nodes second)\s+\d+\b', line, re.IGNORECASE) or re.search(nps_pattern, line, re.IGNORECASE)
        re_bench = re.search(bench_pattern, line, re.IGNORECASE)

        # Set, but don't override
        if re_nps: nps = nps if nps else re_nps.group()
        if re_bench: bench = bench if bench else re_bench.group()

    # Parse out the integer portion from our matches
    nps   = int(re.search(r'\d+', nps  ).group()) if nps   else None
    bench = int(re.search(r'\d+', bench).group()) if bench else None
    return (bench, nps)

def configured_input(options):
    lexer = shlex.shlex(options, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    lexer.escape = ''
    commands = ['uci']
    for option in lexer:
        name, separator, value = option.partition('=')
        if not separator or not name or any(character in option for character in ('\r', '\n', '\0')):
            raise ValueError('Invalid benchmark engine option.')
        commands.append('setoption name %s value %s' % (name, '1' if name.lower() == 'threads' else value))
    return ('\n'.join([*commands, 'isready', 'bench', 'quit']) + '\n').encode('utf-8')


def single_core_bench(binary, outqueue, options=None):

    cmd = ['./%s' % (binary)] + ([] if options is not None else ['bench'])
    process = None

    try: # Launch the bench and wait for results
        payload = configured_input(options) if options is not None else None
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE if options is not None else None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stdout, stderr = process.communicate(payload, timeout=MAX_BENCH_TIME_SECONDS if options is not None else None)
        if options is not None:
            text = stdout.decode('utf-8', errors='replace')
            if process.returncode or not re.search(r'^uciok\s*$', text, re.MULTILINE) or not re.search(r'^readyok\s*$', text, re.MULTILINE) or re.search(r'unknown\s+(?:option|command)|no such option|invalid\s+(?:option|value)|invalidinteger|invalidboolean|unable to (?:set|update)', text, re.IGNORECASE):
                outqueue.put((None, None))
                return
        outqueue.put(parse_stream_output(stdout))

    except: # Signal an error with (None, None)
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        outqueue.put((None, None))

def multi_core_bench(binary, threads, options=None):

    outqueue = multiprocessing.Queue()

    processes = [
        multiprocessing.Process(
            target=single_core_bench, args=(binary, outqueue, options))
        for ii in range(threads)
    ]

    for process in processes:
        process.start()

    try: # Every process deposits exactly one result into the Queue
        return [outqueue.get(timeout=MAX_BENCH_TIME_SECONDS) for ii in range(threads)]

    except queue.Empty: # Force kill the engine, thus causing the processes to finish
        utils.kill_process_by_name(binary)
        raise utils.OpenBenchBadBenchException('[%s] Bench Exceeded Max Duration' % (binary))

    finally: # Join everything to avoid zombie processes
        for process in processes:
            process.join()

def run_benchmark(binary, threads, sets, expected=None, options=None):

    engine = os.path.basename(binary)

    benches, speeds = [], []
    for ii in range(sets):
        for bench, speed in multi_core_bench(binary, threads, options):
            benches.append(bench); speeds.append(speed)

    if None in benches or None in speeds:
        detail = ' Engine must support UCI bench and every configured option.' if options is not None else ''
        raise utils.OpenBenchBadBenchException('[%s] Failed to Execute Benchmark.%s' % (engine, detail))

    if len(set(benches)) != 1:
        raise utils.OpenBenchBadBenchException('[%s] Non-Deterministic Benches' % (engine))

    if expected and expected != benches[0]:
        raise utils.OpenBenchBadBenchException('[%s] Wrong Bench: %d' % (engine, benches[0]))

    return sum(speeds) // len(speeds), benches[0]
