from django import template


register = template.Library()


class LiveRegion(template.Node):
    def __init__(self, name, nodes):
        self.name = name
        self.nodes = nodes

    def render(self, context):
        html = self.nodes.render(context)
        request = context.get('request')
        if hasattr(request, 'live_regions'):
            request.live_regions[self.name.resolve(context)] = html
        return html


@register.tag('live_region')
def live_region(parser, token):
    _, name = token.split_contents()
    nodes = parser.parse(('endlive_region',))
    parser.delete_first_token()
    return LiveRegion(parser.compile_filter(name), nodes)
