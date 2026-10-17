import io
import time

from django.utils import timezone

from OpenBench.models import DatasetUpload


class UploadReader(io.BufferedReader):
    def __init__(self, path, task_id, size):
        super().__init__(io.FileIO(path, 'r'))
        self.task_id = task_id
        self.size = size
        self.reporting = False
        self.high_water = 0
        self.last_report = 0

    def report(self):
        if not self.reporting:
            return
        self.high_water = max(self.high_water, self.tell())
        now = time.monotonic()
        if now - self.last_report >= 2:
            self.last_report = now
            DatasetUpload.objects.filter(pk=self.task_id, state='UPLOADING').update(
                stage='Uploading archive', progress=min(99.9, 100 * self.high_water / max(1, self.size)), updated=timezone.now())

    def read(self, size=-1):
        data = super().read(size)
        self.report()
        return data

    def readinto(self, buffer):
        size = super().readinto(buffer)
        self.report()
        return size
