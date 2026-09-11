from django.core.checks import Error, register
from django.core.exceptions import ValidationError
from django.db import connections


@register()
def check_training_credentials(app_configs, **kwargs):
    from OpenBench.models import HuggingFaceCredential
    from OpenBench.training import hf_token

    connection = connections['default']
    if HuggingFaceCredential._meta.db_table not in connection.introspection.table_names():
        return []
    for user_id in HuggingFaceCredential.objects.values_list('user_id', flat=True):
        try:
            hf_token(user_id)
        except ValidationError:
            return [Error(
                'The configured training encryption key cannot decrypt saved Hugging Face credentials.',
                hint='Restore the original credential key file and use the same MATTBENCH_CREDENTIAL_KEY_FILE for the web server and training_tasks. Do not generate a replacement key.',
                id='OpenBench.E001',
            )]
    return []
