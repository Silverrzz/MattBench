import uuid
from datetime import timedelta

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.debug import sensitive_post_parameters

from OpenBench.discord_notifications import ACTIVE, SNOWFLAKE, canonical_site, decrypt_webhook, encrypt_webhook, validate_webhook
from OpenBench.models import DiscordConfiguration, NotificationDelivery, NotificationPreferences
from OpenBench.notification_events import EVENTS, GROUPS, MODES, default_events


class PreferencesForm(forms.Form):
    enabled = forms.BooleanField(required=False, label='Enable Discord notifications for my account')
    discord_user_id = forms.RegexField(regex=SNOWFLAKE, required=False, max_length=20, label='Discord user ID',
                                      widget=forms.TextInput(attrs={'inputmode': 'numeric', 'autocomplete': 'off'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for key, label in EVENTS.items():
            self.fields[key] = forms.ChoiceField(choices=MODES, label=label)

    def clean_discord_user_id(self):
        value = self.cleaned_data['discord_user_id']
        if value and not SNOWFLAKE.fullmatch(value):
            raise ValidationError('Enter a numeric Discord user ID (17–20 digits).')
        return value

    def clean(self):
        data = super().clean()
        if any(data.get(key) == 'ping' for key in EVENTS) and not data.get('discord_user_id'):
            self.add_error('discord_user_id', 'A Discord user ID is required for Message + ping.')
        return data


def profile_context(user, form=None):
    prefs = NotificationPreferences.objects.filter(user=user).first() or NotificationPreferences(user=user)
    if form is None:
        form = PreferencesForm(initial={**default_events(), **prefs.events, 'enabled': prefs.enabled, 'discord_user_id': prefs.discord_user_id})
    return {'notification_form': form, 'discord': DiscordConfiguration.objects.filter(pk=1).first(),
            'notification_groups': [(family, label, [form[family + '.' + name] for name in names.split()]) for family, (label, names) in GROUPS.items()]}


@login_required(login_url='/login/')
@require_POST
def preferences(request):
    if not request.user.is_active:
        raise PermissionDenied
    form = PreferencesForm(request.POST)
    if not form.is_valid():
        from OpenBench.views import render
        response = render(request, 'profile.html', profile_context(request.user, form))
        response.status_code = 400
        return response
    with transaction.atomic():
        prefs, _ = NotificationPreferences.objects.get_or_create(user=request.user)
        prefs = NotificationPreferences.objects.select_for_update().get(pk=prefs.pk)
        prefs.enabled = form.cleaned_data['enabled']
        prefs.discord_user_id = form.cleaned_data['discord_user_id']
        prefs.events = {key: form.cleaned_data[key] for key in EVENTS}
        prefs.save()
        pending = NotificationDelivery.objects.filter(recipient=request.user, status__in=ACTIVE).exclude(event_key='admin.test')
        if prefs.enabled:
            pending = pending.filter(event_key__in=[key for key, mode in prefs.events.items() if mode == 'off'])
        pending.update(status='skipped', last_error='Account or event notifications disabled.')
        # A removed ping can never come back on an already queued message.
        NotificationDelivery.objects.filter(recipient=request.user, status__in=ACTIVE).exclude(
            event_key__in=[key for key, mode in prefs.events.items() if mode == 'ping']).update(ping_requested=False)
    request.session['status_message'] = 'Discord notification preferences saved.'
    return redirect('/profile/#discord-notifications')


class ConfigurationForm(forms.Form):
    enabled = forms.BooleanField(required=False, label='Enable platform Discord notifications')
    destination_label = forms.CharField(max_length=128, label='Destination label')
    canonical_url = forms.URLField(label='Canonical Mattbench URL')
    webhook = forms.CharField(required=False, max_length=400, label='Replace webhook URL',
                              widget=forms.PasswordInput(attrs={'autocomplete': 'new-password', 'spellcheck': 'false', 'placeholder': 'Leave blank to keep the saved webhook'}))

    def clean_canonical_url(self):
        return canonical_site(self.cleaned_data['canonical_url'])


@login_required(login_url='/login/')
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters('webhook')
def configuration(request):
    if not request.user.is_active or not request.user.is_superuser:
        raise PermissionDenied
    config, _ = DiscordConfiguration.objects.get_or_create(pk=1)
    form = ConfigurationForm(initial={'enabled': config.enabled, 'destination_label': config.destination_label, 'canonical_url': config.canonical_url})
    errors = []
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'save':
                form = ConfigurationForm(request.POST)
                if not form.is_valid():
                    raise ValidationError('Correct the configuration fields below.')
                data = form.cleaned_data
                replacement = data['webhook']
                # Verify before taking database locks. Never display or persist the plaintext.
                verified = validate_webhook(replacement) if replacement else None
                encrypted = encrypt_webhook(replacement) if replacement else None
                with transaction.atomic():
                    config = DiscordConfiguration.objects.select_for_update().get(pk=1)
                    if not encrypted and not config.webhook_ciphertext:
                        raise ValidationError('Configure and validate a webhook before saving.')
                    config.enabled = data['enabled']
                    config.destination_label = data['destination_label']
                    config.canonical_url = data['canonical_url']
                    if encrypted:
                        config.webhook_ciphertext = encrypted
                        config.guild_id, config.channel_id = verified
                        config.generation = uuid.uuid4()
                        config.next_send_at = timezone.now()
                        NotificationDelivery.objects.filter(status__in=ACTIVE).update(status='skipped', last_error='Webhook configuration replaced.')
                    elif not config.enabled:
                        NotificationDelivery.objects.filter(status__in=ACTIVE).exclude(event_key='admin.test').update(status='skipped', last_error='Platform notifications disabled.')
                    config.save()
                request.session['status_message'] = 'Discord configuration saved and webhook validated.' if encrypted else 'Discord configuration saved.'
            elif action == 'validate':
                generation = config.generation
                guild, channel = validate_webhook(decrypt_webhook(config))
                with transaction.atomic():
                    config = DiscordConfiguration.objects.select_for_update().get(pk=1)
                    if config.generation != generation:
                        raise ValidationError('Configuration changed. Validate again.')
                    if (guild, channel) != (config.guild_id, config.channel_id):
                        config.generation = uuid.uuid4()
                        NotificationDelivery.objects.filter(status__in=ACTIVE).update(status='skipped', last_error='Webhook destination changed.')
                    config.guild_id, config.channel_id = guild, channel
                    config.save()
                request.session['status_message'] = 'Discord webhook validated.'
            elif action == 'test':
                with transaction.atomic():
                    config = DiscordConfiguration.objects.select_for_update().get(pk=1)
                    if not config.webhook_ciphertext or not config.canonical_url:
                        raise ValidationError('Save and validate a webhook and canonical Mattbench URL first.')
                    NotificationDelivery.objects.create(recipient=request.user, event_key='admin.test', subject_key='admin.test', generation=config.generation,
                        summary={'title': 'Mattbench Discord notification test', 'description': 'Administrator-requested delivery check. No mentions.',
                                 'url': config.canonical_url, 'color': 0x3498DB, 'timestamp': timezone.now().isoformat()})
                request.session['status_message'] = 'Test message queued. The Discord sender will deliver it even while platform notifications are disabled.'
            elif action == 'remove':
                with transaction.atomic():
                    config = DiscordConfiguration.objects.select_for_update().get(pk=1)
                    config.enabled = False
                    config.webhook_ciphertext = config.guild_id = config.channel_id = config.destination_label = ''
                    config.generation = uuid.uuid4()
                    config.save()
                    NotificationDelivery.objects.filter(status__in=ACTIVE).update(status='skipped', last_error='Webhook configuration removed.')
                request.session['status_message'] = 'Discord configuration removed.'
            else:
                raise ValidationError('Unknown notification action.')
            return redirect('/manage/notifications/')
        except ValidationError as error:
            errors = error.messages
    from OpenBench.configuration_views import SECTIONS
    from OpenBench.views import render
    response = render(request, 'notifications.html', {'page_title': 'Notifications', 'discord': config, 'form': form, 'errors': errors,
        'navigation': [(key, label) for key, (label, _) in SECTIONS.items() if key != 'releases'],
        'sender_healthy': bool(config.sender_seen and config.sender_seen > timezone.now() - timedelta(minutes=2)),
        'pending_count': NotificationDelivery.objects.filter(status__in=ACTIVE).count(),
        'failures': NotificationDelivery.objects.exclude(last_error='').exclude(status='skipped').order_by('-id')[:10]})
    response['Cache-Control'] = 'no-store'
    if errors:
        response.status_code = 400
    return response
