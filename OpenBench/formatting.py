def format_nps(nps):
    if nps is None or nps <= 0:
        return '\u2014'
    for scale, suffix in ((1e9, 'Gnps'), (1e6, 'Mnps'), (1e3, 'Knps')):
        if nps >= scale:
            return ('%.1f' % (nps / scale)).rstrip('0').rstrip('.') + suffix
    return '%.0fnps' % nps
