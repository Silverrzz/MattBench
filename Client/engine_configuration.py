import math
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from collections import deque

import utils


class ConfigurationError(Exception):
    pass


def runner_warning(line):
    return bool(re.search(r"(?:Warning;.*(?:doesn't have option|Invalid value for option|Failed to set (?:option|UCI_Chess960)))|(?:Engine .*does not support UCI_Variant)", line, re.IGNORECASE))


def fail(config, report, message, logs=''):
    report(config, 'clientSubmitError', {'test_id': config.workload['test']['id'], 'error': ('Engine configuration invalid: ' + message)[:1024], 'logs': logs[-65536:], 'fatal': 'configuration'})
    raise utils.OpenBenchFatalWorkerException(message)


def stop_runners(config):
    for runner in list(getattr(config, 'configuration_runners', [])):
        if runner.poll() is None:
            try:
                runner.terminate()
            except OSError:
                pass


def track_runner(config, runner, abort_flag):
    if not hasattr(config, 'configuration_runners'):
        config.configuration_runners = []
    config.configuration_runners.append(runner)
    if abort_flag.is_set():
        stop_runners(config)
        try:
            runner.wait(timeout=10)
        except subprocess.TimeoutExpired:
            runner.kill()
            runner.wait()
        return False
    return True


def parse_options(settings):
    result = []
    for token in shlex.split(settings):
        if not token.startswith('option.'):
            continue
        name, separator, value = token[7:].partition('=')
        if not separator or not name or any(character in name + value for character in ('\r', '\n', '\0')):
            raise ConfigurationError('Invalid option in match-runner configuration.')
        result.append((name, value))
    return result


def definitions(lines):
    result = {}
    for line in lines:
        match = re.fullmatch(r'option name (.+?) type (spin|check|combo|string|button)(?: (.*))?', line)
        if match:
            result[match[1]] = (match[2], match[3] or '')
    return result


def validate_value(name, value, available):
    if name not in available:
        raise ConfigurationError('The engine does not advertise option ' + name + '; Fastchess would skip it.')
    kind, specification = available[name]
    valid = True
    if kind == 'spin':
        bounds = {key: number for key, number in re.findall(r'(?:^|\s)(default|min|max)\s+(\S+)', specification)}
        try:
            integer = all(re.fullmatch(r'[+-]?\d+', bounds[key]) and -2147483648 <= int(bounds[key]) <= 2147483647 for key in ('default', 'min', 'max'))
            number = float(value)
            valid = math.isfinite(number) and float(bounds['min']) <= number <= float(bounds['max']) and (not integer or bool(re.fullmatch(r'[+-]?\d+', value)))
        except (KeyError, ValueError, OverflowError):
            valid = False
    elif kind in ('check', 'button'):
        valid = value in ('true', 'false') if kind == 'check' else value == 'true'
    elif kind == 'combo':
        valid = value in re.findall(r'(?:^|\s)var\s+(.+?)(?=\s+var\s+|$)', specification)
    if not valid:
        raise ConfigurationError('Invalid value for option %s: %s.' % (name, value))


def check_engine(binary, groups, variant):
    process = subprocess.Popen([os.path.abspath(binary)], cwd=os.path.dirname(os.path.abspath(binary)), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1)
    output, transcript = queue.Queue(maxsize=16384), deque(maxlen=256)
    closed = threading.Event()
    deadline = time.monotonic() + 60
    def read():
        try:
            for line in process.stdout:
                while not closed.is_set():
                    try:
                        output.put(line.strip(), timeout=.1)
                        break
                    except queue.Full:
                        continue
        finally:
            closed.set()
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    watchdog = threading.Timer(60, lambda: process.kill() if process.poll() is None else None)
    watchdog.daemon = True
    watchdog.start()
    def send(command):
        process.stdin.write(command + '\n')
        process.stdin.flush()
    def receive(marker):
        lines = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConfigurationError('Engine configuration handshake timed out.')
            try:
                line = output.get(timeout=min(.25, remaining))
            except queue.Empty:
                if closed.is_set():
                    raise ConfigurationError('Engine exited during configuration validation.')
                continue
            transcript.append(line)
            if not line.startswith(('option name ', 'id ')) and re.search(r'unknown\s+(?:option|command)|no such option|invalid\s+(?:option|value|uci_variant)|invalidinteger|invalidboolean|unable to (?:set|update)', line, re.IGNORECASE):
                raise ConfigurationError('The engine rejected its configuration: ' + line[-512:])
            if line == marker:
                return lines
            lines.append(line)
            if len(lines) > 16384:
                raise ConfigurationError('Engine configuration output exceeded its limit.')
    try:
        send('uci')
        available = definitions(receive('uciok'))
        selected = []
        for group in groups:
            automatic = []
            if variant in ('duck', 'duck960'):
                if 'UCI_Variant' in available:
                    automatic.append(('UCI_Variant', 'duck'))
                if 'UCI_Chess960' in available:
                    automatic.append(('UCI_Chess960', 'true' if variant == 'duck960' else 'false'))
            elif variant in ('fischerandom', 'chess960', 'frc'):
                automatic.append(('UCI_Chess960', 'true'))
            values = [*group, *automatic]
            for name, value in values:
                validate_value(name, value, available)
            if values not in selected:
                selected.append(values)
        applied = {}
        for values in selected:
            values = sorted(values, key=lambda item: item[0] != 'Threads')
            for name, value in values:
                if applied.get(name) != value or available[name][0] == 'button':
                    send('setoption name %s%s' % (name, '' if available[name][0] == 'button' else ' value ' + value))
                    applied[name] = value
            send('ucinewgame')
            send('isready')
            receive('readyok')
        send('quit')
        process.wait(timeout=max(.1, deadline - time.monotonic()))
        if process.returncode:
            raise ConfigurationError('Engine failed while accepting its configuration.')
        return len(selected)
    except ConfigurationError as error:
        raise ConfigurationError(str(error) + '\n' + '\n'.join(transcript)[-8192:]) from None
    finally:
        watchdog.cancel()
        closed.set()
        if process.poll() is None:
            process.kill()
        process.wait()
        reader.join(timeout=1)
        process.stdin.close()
        process.stdout.close()


def validate(config, engines, runner, report):
    config.configuration_runners = []
    for branch, engine in engines.items():
        try:
            groups = [parse_options(runner.engine_settings(config, engine, branch, 1.0, index)) for index in range(config.workload['distribution']['runner-count'])]
            count = check_engine(os.path.join('Engines', engine), groups, config.workload['test'].get('variant', 'standard'))
        except (ConfigurationError, OSError, ValueError, subprocess.SubprocessError) as error:
            fail(config, report, '%s: %s' % (branch, error), str(error))
        print('Validated %d runtime option configurations for %s.' % (count, branch))
