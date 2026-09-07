from django import forms
from django.contrib.auth import get_user_model

from OpenBench.configuration_schema import SCHEMAS
from OpenBench.models import EngineConfig, OpeningBook, Runner, RunnerRelease, SiteSettings, Variant


class StringListField(forms.CharField):
    def to_python(self, value):
        return list(dict.fromkeys(line.strip() for line in (value or '').splitlines() if line.strip()))

    def prepare_value(self, value):
        return '\n'.join(value) if isinstance(value, list) else value


class ConfigurationForm(forms.Form):
    generation = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, instance, generation, user, data=None):
        super().__init__(data=data, initial={'generation': generation})
        self.instance = instance
        if not isinstance(instance, SiteSettings):
            self.fields['name'] = forms.CharField(max_length=128, initial=instance.name,
                disabled=not instance._state.adding and isinstance(instance, (EngineConfig, OpeningBook, Variant)))
            self.fields['enabled'] = forms.BooleanField(required=False, initial=instance.enabled)
        relations = {OpeningBook: ('variant', Variant), Variant: ('runner_release', RunnerRelease), RunnerRelease: ('runner', Runner)}
        if type(instance) in relations:
            field, model = relations[type(instance)]
            self.fields[field] = forms.ModelChoiceField(model.objects.order_by('name'), initial=getattr(instance, field + '_id'),
                disabled=isinstance(instance, RunnerRelease) and not instance._state.adding)
        if isinstance(instance, EngineConfig):
            self.fields['variants'] = forms.ModelMultipleChoiceField(Variant.objects.order_by('name'), required=False,
                initial=instance.variants.all() if not instance._state.adding else [], widget=forms.CheckboxSelectMultiple, help_text='Only select variants this engine can play.')
            if user.is_superuser:
                self.fields['maintainers'] = forms.ModelMultipleChoiceField(get_user_model().objects.filter(is_active=True).order_by('username'), required=False,
                    initial=instance.maintainers.values_list('user_id', flat=True) if not instance._state.adding else [], widget=forms.CheckboxSelectMultiple, help_text='Maintainers can change this engine and its shared presets.')
        self.schema = SCHEMAS[instance._meta.model_name]
        self.add_settings(self.schema, instance.settings)

        labels = {
            'name': ('Name', 'Use a recognizable name. Engine, book and variant names cannot be changed after saving.'),
            'nps': ('Reference NPS', 'Nodes per second used to scale time controls. A draft may use 0.'),
            'source': ('Repository / download URL', 'Use an HTTPS URL.'),
            'private': ('Private engine', ''),
            'build__path': ('Build directory', 'Relative to the repository root. Leave blank for the root.'),
            'build__compilers': ('Compilers', 'One compiler per line, for example gcc or clang.'),
            'build__systems': ('Operating systems', 'One system per line: Linux or Windows.'),
            'build__cpuflags': ('Required CPU features', 'One feature per line, for example AVX2. Leave blank if none are required.'),
            'ref': ('Branch or tag', 'The source reference for this release.'),
            'commit': ('Pinned commit', 'Optional full commit SHA. Takes precedence over the branch or tag.'),
            'fastchess_variant': ('Fastchess variant name', 'The name accepted by the runner with -variant, for example duck.'),
            'syzygy': ('Supports Syzygy', 'Enable only when this variant supports Syzygy tablebases.'),
            'sha': ('Book SHA-256', 'Checksum of the extracted book, not its ZIP download.'),
        }
        for name, (label, help_text) in labels.items():
            if name in self.fields:
                self.fields[name].label = label
                self.fields[name].help_text = help_text
        if 'source' in self.fields:
            self.fields['source'].label = 'Download URL' if isinstance(instance, OpeningBook) else 'Repository URL'
        if 'nps' in self.fields and self.fields['nps'].initial is None:
            self.fields['nps'].initial = 0
        for field in self.fields.values():
            field.help_text = ''
            if isinstance(field.widget, forms.CheckboxSelectMultiple):
                field.widget.attrs['class'] = 'choice-list'

    def groups(self):
        definitions = [
            ('Identity', 'How this entry appears in MattBench.', ('name', 'enabled', 'source', 'private')),
            ('Game support', 'Connect the engine, opening book and match runner.', ('variants', 'variant', 'runner', 'runner_release', 'fastchess_variant', 'syzygy')),
            ('Performance & build', 'Requirements used to build engines and scale testing speed.', ('nps', 'build__path', 'build__compilers', 'build__systems', 'build__cpuflags', 'build__target')),
            ('Runner version', 'A saved release keeps its version settings. Create another release to update them.', ('ref', 'commit', 'min_version', 'protocol')),
            ('Opening data', 'Workers download this book and verify its checksum.', ('format', 'sha')),
            ('Maintainers', 'Choose who can edit this engine.', ('maintainers',)),
            ('Access & approvals', 'Control who can join and use this installation.', ('require_login_to_view', 'require_manual_registration', 'use_cross_approval')),
            ('Worker updates', 'The client and default runner distributed to workers.', ('client_version', 'client_repo_url', 'client_repo_ref', 'fastchess_min_version', 'fastchess_repo_url', 'fastchess_repo_ref')),
            ('Scheduling & downloads', 'Installation-wide workload and download settings.', ('balance_engine_throughputs', 'use_x_accel_redirect', 'x_accel_redirect_root')),
        ]
        used = {'generation'}
        groups = []
        for title, description, names in definitions:
            fields = [self[name] for name in names if name in self.fields]
            used.update(names)
            if fields:
                groups.append((title, description, fields))
        remaining = [self[name] for name in self.fields if name not in used]
        if remaining:
            groups.append(('Other settings', '', remaining))
        return groups

    def columns(self):
        groups = self.groups()
        midpoint = (len(groups) + 1) // 2
        return tuple(column for column in (groups[:midpoint], groups[midpoint:]) if column)

    def add_settings(self, schema, values, prefix=''):
        for key, spec in schema['properties'].items():
            field = prefix + key
            if spec.get('type') == 'object':
                self.add_settings(spec, values.get(key, {}), field + '__')
                continue
            options = {'label': field.replace('__', ' / ').replace('_', ' ').capitalize(), 'initial': values.get(key),
                'required': key in schema.get('required', []) and spec.get('type') != 'boolean'}
            if 'enum' in spec:
                choices = [(value, value) for value in spec['enum']]
                self.fields[field] = forms.ChoiceField(choices=choices, **options)
            elif spec['type'] == 'boolean':
                self.fields[field] = forms.BooleanField(**options)
            elif spec['type'] == 'integer':
                self.fields[field] = forms.IntegerField(min_value=spec.get('minimum', 0), **options)
            elif spec['type'] == 'array':
                options['required'] = False
                self.fields[field] = StringListField(widget=forms.Textarea(attrs={'rows': 3}), help_text='One value per line.', **options)
            else:
                options['required'] = options['required'] and ('minLength' in spec or 'pattern' in spec)
                self.fields[field] = forms.CharField(**options)
            if isinstance(self.instance, RunnerRelease) and not self.instance._state.adding:
                self.fields[field].disabled = True

    def settings_data(self, schema=None, prefix=''):
        schema = schema or self.schema
        result = {}
        for key, spec in schema['properties'].items():
            field = prefix + key
            value = self.settings_data(spec, field + '__') if spec.get('type') == 'object' else self.cleaned_data[field]
            if key in schema.get('required', []) or value not in ('', None, {}, []):
                result[key] = value
        return result

    def populate(self):
        for field in ('name', 'enabled', 'variant', 'runner_release', 'runner'):
            if field in self.cleaned_data:
                setattr(self.instance, field, self.cleaned_data[field])
        self.instance.settings = self.settings_data()
        return self.instance
