# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
#                                                                             #
#   OpenBench is a chess engine testing framework authored by Andrew Grant.   #
#   <https://github.com/AndyGrant/OpenBench>           <andrew@grantnet.us>   #
#                                                                             #
#   OpenBench is free software: you can redistribute it and/or modify         #
#   it under the terms of the GNU General Public License as published by      #
#   the Free Software Foundation, either version 3 of the License, or         #
#   (at your option) any later version.                                       #
#                                                                             #
#   OpenBench is distributed in the hope that it will be useful,              #
#   but WITHOUT ANY WARRANTY; without even the implied warranty of            #
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the             #
#   GNU General Public License for more details.                              #
#                                                                             #
#   You should have received a copy of the GNU General Public License         #
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.     #
#                                                                             #
# # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

from django.db.models import CharField, IntegerField, BigIntegerField, BooleanField, FloatField
from django.db.models import JSONField, ForeignKey, DateTimeField, OneToOneField, TextField
from django.db.models import CASCADE, PROTECT, Model, TextChoices, UniqueConstraint
from django.contrib.auth.models import User

class Engine(Model):

    name     = CharField(max_length=128)
    source   = CharField(max_length=1024)
    sha      = CharField(max_length=64)
    bench    = IntegerField(default=0)

    def __str__(self):
        return '{0} ({1})'.format(self.name, self.bench)

class Profile(Model):

    user     = ForeignKey(User, PROTECT, related_name='user')
    games    = BigIntegerField(default=0)
    tests    = IntegerField(default=0)
    repos    = JSONField(default=dict, blank=True, null=True)
    engine   = CharField(max_length=128, blank=True)
    enabled  = BooleanField(default=False)
    approver = BooleanField(default=False)
    updated  = DateTimeField(auto_now=True)

    def __str__(self):
        return self.user.__str__()

class Machine(Model):

    user      = ForeignKey(User, PROTECT, related_name='owner')
    mnps      = FloatField(default=0.00)
    dev_mnps  = FloatField(default=0.00)
    base_mnps = FloatField(default=0.00)
    updated   = DateTimeField(auto_now=True, db_index=True)
    secret    = CharField(max_length=64, default='None')
    info      = JSONField()
    workload  = IntegerField(default=0)
    mode = CharField(max_length=16, default='automatic', choices=[('automatic', 'Automatic'), ('testing-only', 'Testing only'), ('training-only', 'Training only'), ('paused', 'Paused')])

    def __str__(self):
        return '[%d] %s' % (self.id, self.user.username)

class Result(Model):

    test     = ForeignKey('Test', PROTECT, related_name='test')
    machine  = ForeignKey('Machine', PROTECT, related_name='machine')
    updated  = DateTimeField(auto_now=True)

    # Trinomial Distributions
    losses = IntegerField(default=0)
    draws  = IntegerField(default=0)
    wins   = IntegerField(default=0)

    # Pentanomial Distributions
    LL = IntegerField(default=0)
    LD = IntegerField(default=0)
    DD = IntegerField(default=0)
    DW = IntegerField(default=0)
    WW = IntegerField(default=0)

    # Overall collection of Results
    games    = IntegerField(default=0)
    crashes  = IntegerField(default=0)
    timeloss = IntegerField(default=0)

    # Total counters for nodes and ms for NPS tracking
    dev_nodes         = BigIntegerField(default=0)
    dev_time          = BigIntegerField(default=0)
    dev_time_scaled   = BigIntegerField(default=0)
    base_nodes        = BigIntegerField(default=0)
    base_time         = BigIntegerField(default=0)
    base_time_scaled  = BigIntegerField(default=0)

    def __str__(self):
        return '{0} {1}'.format(self.test.dev.name, self.machine.__str__())

class Test(Model):

    class ScaleMethod(TextChoices):
        DEV  = 'DEV' , 'DEV'
        BASE = 'BASE', 'BASE'
        BOTH = 'BOTH', 'BOTH'

    # Misc information
    author      = CharField(max_length=64)
    upload_pgns = CharField(max_length=16, default='FALSE')
    info        = CharField(max_length=1024, default='', blank=True)

    # Opening book settings
    book_name  = CharField(max_length=32)
    book_index = IntegerField(default=1)

    # Dev Engine, and all of its settings
    dev              = ForeignKey('Engine', PROTECT, related_name='dev')
    dev_repo         = CharField(max_length=1024)
    dev_engine       = CharField(max_length=64)
    dev_options      = TextField()
    dev_network      = CharField(max_length=256, blank=True)
    dev_netname      = CharField(max_length=256, blank=True)
    dev_time_control = CharField(max_length=32)

    # Base Engine, and all of its settings
    base              = ForeignKey('Engine', PROTECT, related_name='base')
    base_repo         = CharField(max_length=1024)
    base_engine       = CharField(max_length=64)
    base_options      = TextField()
    base_network      = CharField(max_length=256, blank=True)
    base_netname      = CharField(max_length=256, blank=True)
    base_time_control = CharField(max_length=32)

    # Changable Test Parameters
    workload_size = IntegerField(default=32)
    priority      = IntegerField(default=0)
    throughput    = IntegerField(default=0)

    # Scaling Mechanisms
    scale_method  = CharField(max_length=16, choices=ScaleMethod.choices, default=ScaleMethod.BASE)
    scale_nps     = IntegerField(default=0)

    # Tablebases and Match runner adjudicatoins
    syzygy_wdl  = CharField(max_length=16, default='OPTIONAL')
    syzygy_adj  = CharField(max_length=16, default='OPTIONAL')
    win_adj     = CharField(max_length=64, default='movecount=3 score=400')
    draw_adj    = CharField(max_length=64, default='movenumber=40 movecount=8 score=10')

    # Test Mode specific values, either SPRT, GAMES, SPSA, or DATAGEN
    test_mode     = CharField(max_length=16, default='SPRT')
    elolower      = FloatField(default=0.0) # SPRT
    eloupper      = FloatField(default=0.0) # SPRT
    alpha         = FloatField(default=0.0) # SPRT
    beta          = FloatField(default=0.0) # SPRT
    lowerllr      = FloatField(default=0.0) # SPRT
    currentllr    = FloatField(default=0.0) # SPRT
    upperllr      = FloatField(default=0.0) # SPRT
    llr_history_state = JSONField(default=dict, blank=True)
    execution = JSONField(default=dict, blank=True)
    max_games     = BigIntegerField(default=0)
    genfens_args  = CharField(max_length=256, default='', blank=True) # DATAGEN
    play_reverses = BooleanField(default=False) # DATAGEN

    # Collection of all individual Result() objects
    games  = IntegerField(default=0) # Overall
    losses = IntegerField(default=0) # Trinomial
    draws  = IntegerField(default=0) # Trinomial
    wins   = IntegerField(default=0) # Trinomial
    LL     = IntegerField(default=0) # Pentanomial
    LD     = IntegerField(default=0) # Pentanomial
    DD     = IntegerField(default=0) # Pentanomial
    DW     = IntegerField(default=0) # Pentanomial
    WW     = IntegerField(default=0) # Pentanomial

    # Switching all future tests to Pentanomial
    use_tri   = BooleanField(default=False)
    use_penta = BooleanField(default=True)

    # All status flags associated with the test
    passed      = BooleanField(default=False)
    failed      = BooleanField(default=False)
    finished    = BooleanField(default=False)
    deleted     = BooleanField(default=False)
    approved    = BooleanField(default=False)
    error       = BooleanField(default=False)

    # Datetime house keeping for meta data
    creation    = DateTimeField(auto_now_add=True)
    updated     = DateTimeField(auto_now=True)

    def __str__(self):
        return '{0} vs {1} @ {2}'.format(self.dev.name, self.base.name, self.dev_time_control)

    def results(self):
        return self.as_tri() if self.use_tri else self.as_penta()

    def as_tri(self):
        return (self.losses, self.draws, self.wins)

    def as_penta(self):
        return (self.LL, self.LD, self.DD, self.DW, self.WW)

    def as_nwld(self):
        return (self.games, self.wins, self.losses, self.draws)

    def workload_type_str(self):
        return {'SPSA' : 'tune', 'DATAGEN' : 'datagen'}.get(self.test_mode, 'test')

class LLRHistory(Model):

    test = ForeignKey(Test, CASCADE, related_name='llr_history')
    games = IntegerField()
    llr = FloatField()

    class Meta:
        ordering = ['games']
        constraints = [UniqueConstraint(fields=['test', 'games'], name='unique_test_llr_games')]

class LogEvent(Model):

    author     = CharField(max_length=128) # Username for the OpenBench Profile
    summary    = CharField(max_length=128) # Quick summary of the Event or Error
    log_file   = CharField(max_length=128) # .log file stored in /Media/

    machine_id = IntegerField(default=0)   # Only set for Client based Log Events
    test_id    = IntegerField(default=0)   # Should always be set

    created    = DateTimeField(auto_now_add=True)

    def __str__(self):
        return "{0} {1} {2}".format(self.author, str(self.test_id), self.summary)

class Network(Model):

    default     = BooleanField(default=False)
    was_default = BooleanField(default=False)
    sha256      = CharField(max_length=8)
    name        = CharField(max_length=64)
    engine      = CharField(max_length=64)
    author      = CharField(max_length=64)
    created     = DateTimeField(auto_now_add=True)

    def __str__(self):
        return '[{}] {} ({})'.format(self.engine, self.name, self.sha256)

class PGN(Model):

    test_id    = IntegerField(default=0)
    result_id  = IntegerField(default=0)
    book_index = IntegerField(default=0)
    processed  = BooleanField(default=False)

    def __str__(self):
        return self.filename()

    def filename(self):
        return '%s.%s.%s.pgn.bz2' % (self.test_id, self.result_id, self.book_index)

class SPSARun(Model):

    class SPSAReportingType(TextChoices):
        BULK    = 'BULK'   , 'BULK'
        BATCHED = 'BATCHED', 'BATCHED'

    class SPSADistributionType(TextChoices):
        SINGLE   = 'SINGLE'  , 'SINGLE'
        MULTIPLE = 'MULTIPLE', 'MULTIPLE'

    tune = OneToOneField(Test, on_delete=CASCADE, related_name='spsa_run', null=True, blank=True)

    reporting_type    = CharField(max_length=16, choices=SPSAReportingType.choices)
    distribution_type = CharField(max_length=16, choices=SPSADistributionType.choices)

    alpha      = FloatField() # Constants
    gamma      = FloatField()
    iterations = IntegerField()
    pairs_per  = IntegerField()
    a_ratio    = FloatField()

class SPSAParameter(Model):

    spsa_run  = ForeignKey(SPSARun, on_delete=CASCADE, related_name='parameters')
    name      = CharField(max_length=64)
    index     = IntegerField()
    value     = FloatField() # Only field that changes

    is_float  = BooleanField() # Constants
    start     = FloatField()
    min_value = FloatField()
    max_value = FloatField()
    c_end     = FloatField()
    r_end     = FloatField()

    c_value   = FloatField() # Constants pre-computed for speed
    a_value   = FloatField()

import uuid

from django.db.models import UUIDField, PositiveIntegerField, ManyToManyField, Q
from django.core.exceptions import ValidationError


class Runner(Model):

    id       = UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name     = CharField(max_length=128, unique=True)
    enabled  = BooleanField(default=False)
    settings = JSONField(default=dict, blank=True)
    updated  = DateTimeField(auto_now=True)

    def clean(self):
        from OpenBench.config import verify_runner_config
        verify_runner_config(self.settings)

    def __str__(self):
        return self.settings.get('source', '').removeprefix('https://github.com/').rstrip('/') if self.name.startswith('import-') else self.name


class RunnerRelease(Model):

    id       = UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name     = CharField(max_length=128, unique=True)
    enabled  = BooleanField(default=False)
    settings = JSONField(default=dict, blank=True)
    updated  = DateTimeField(auto_now=True)
    runner   = ForeignKey(Runner, on_delete=PROTECT, related_name='releases')

    def clean(self):
        from OpenBench.config import verify_release_config
        verify_release_config(self.settings)
        if self.enabled and not self.runner.enabled:
            raise ValidationError('Enable the runner before enabling its release')

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            if (original.runner_id, original.settings) != (self.runner_id, self.settings):
                raise ValidationError('Create a new release to change execution settings')
        return super().save(*args, **kwargs)

    def __str__(self):
        return '%s / %s' % (self.runner, self.settings.get('ref', '')[:12]) if self.name.startswith('import-') else self.name


class Variant(Model):

    id             = UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name           = CharField(max_length=128, unique=True)
    enabled        = BooleanField(default=False)
    settings       = JSONField(default=dict, blank=True)
    updated        = DateTimeField(auto_now=True)
    runner_release = ForeignKey(RunnerRelease, on_delete=PROTECT)

    def clean(self):
        from OpenBench.config import verify_variant_config
        verify_variant_config(self.settings)
        if self.enabled and not (self.runner_release.enabled and self.runner_release.runner.enabled):
            raise ValidationError('Choose an enabled runner release')

    def __str__(self):
        return self.name


class EngineConfig(Model):

    id       = UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name     = CharField(max_length=128, unique=True)
    enabled  = BooleanField(default=False)
    settings = JSONField(default=dict, blank=True)
    updated  = DateTimeField(auto_now=True)
    variants = ManyToManyField(Variant, related_name='engines', blank=True)

    def clean(self):
        from OpenBench.config import verify_engine_config
        verify_engine_config(self.settings, self.enabled)

    def __str__(self):
        return self.name


class OpeningBook(Model):

    id       = UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name     = CharField(max_length=128, unique=True)
    enabled  = BooleanField(default=False)
    settings = JSONField(default=dict, blank=True)
    updated  = DateTimeField(auto_now=True)
    variants = ManyToManyField(Variant, related_name='books', blank=True)

    def clean(self):
        from OpenBench.config import verify_book_config
        verify_book_config(self.name, self.settings)

    def __str__(self):
        return self.name


class WorkloadPreset(Model):

    id            = UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    engine        = ForeignKey(EngineConfig, on_delete=PROTECT, related_name='presets')
    owner         = ForeignKey(User, null=True, blank=True, on_delete=CASCADE)
    name          = CharField(max_length=128)
    workload_type = CharField(max_length=8, choices=[('TEST', 'Test'), ('TUNE', 'Tune'), ('DATAGEN', 'Datagen')])
    position      = PositiveIntegerField(default=0)
    settings      = JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['position', 'name', 'id']
        constraints = [
            UniqueConstraint(fields=['engine', 'workload_type', 'name'], condition=Q(owner__isnull=True), name='unique_shared_preset'),
            UniqueConstraint(fields=['engine', 'owner', 'workload_type', 'name'], condition=Q(owner__isnull=False), name='unique_personal_preset'),
        ]

    def clean(self):
        from OpenBench.config import verify_preset
        verify_preset(self.workload_type, self.settings)


from OpenBench.training_models import HuggingFaceCredential, TrainingSchedule, TrainingWorker
from OpenBench.training_models import TrainingRun, TrainingArtifact, DatasetUpload
from OpenBench.training_models import TrainingCheckpoint, LifecycleEvent
from OpenBench.training_models import TrainingDataset
from OpenBench.training_models import TrainingServiceLease
