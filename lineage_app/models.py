from decimal import Decimal
import gzip
import os
import logging
import re
import shutil
import tempfile
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator
from django.dispatch import receiver
from django.db import models
from django.db.models.signals import pre_delete
from django.urls import reverse
from lineage import Lineage, save_df_as_csv
from lineage.snps import SNPs
import pandas as pd

from .storage import SendFileFileSystemStorage

User = get_user_model()

logger = logging.getLogger(__name__)

sendfile_storage = SendFileFileSystemStorage()


def parse_snps(file):
    summary_info = None
    is_valid = False
    try:
        # just getting summary, don't need to assign PAR SNPs to a chromosome
        snps = SNPs(file, assign_par_snps=False)
        summary_info = snps.get_summary()
        is_valid = snps.is_valid()
    except Exception as err:
        logger.error(err)

    return summary_info, is_valid


def get_relative_user_dir(user_uuid):
    """ Get path relative to `SENDFILE_ROOT`. """
    return settings.USERS_DIR + '/{}'.format(str(user_uuid))


def get_absolute_user_dir(user_uuid):
    return os.path.join(settings.SENDFILE_ROOT, get_relative_user_dir(user_uuid))


def get_relative_user_dir_file(user_uuid, obj_uuid, ext=''):
    """ Get path relative to `SENDFILE_ROOT`. """
    return get_relative_user_dir(user_uuid) + '/{}{}'.format(str(obj_uuid), ext)


def remove_user_dir_if_empty(user_uuid):
    user_dir = get_absolute_user_dir(user_uuid)
    if os.path.exists(user_dir):
        if not os.listdir(user_dir):
            os.rmdir(user_dir)


def clean_string(s):
    """ Clean a string so that it can be a valid Python variable name.

    Parameters
    ----------
    s : str
        string to clean

    Returns
    -------
    str
        cleaned string that can be used as a variable name
    """
    # http://stackoverflow.com/a/3305731
    return re.sub('\W|^(?=\d)', '_', s)


def compress_file(path_in, path_out):
    # https://stackoverflow.com/a/25729514
    with open(path_in, 'rb') as f_in:
        with open(path_out, 'wb') as f_out:
            gz = gzip.GzipFile('', 'wb', 9, f_out, 0.)
            gz.write(f_in.read())
            gz.close()


class Individual(models.Model):
    uuid = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='individuals')
    name = models.CharField(max_length=256)
    openhumans_individual = models.BooleanField(default=False, editable=False)
    locked = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, editable=False)

    def __str__(self):
        return str(self.name)

    def delete(self, *args, **kwargs):
        snps_all = self.snps.all()
        for snps in snps_all:
            snps.delete()

        remove_user_dir_if_empty(self.user.uuid)

        super().delete(*args, **kwargs)

    def snps_can_be_merged(self):
        # if self.snps.all().count() <= 1:
        #     return False
        #
        # if self.merging_in_progress:
        #     return False
        #
        # lineage_snps = self.snps.filter(generated_by_lineage=True)
        #
        # # all SNP files have already been merged
        # if len(self.snps.filter(merged=False)) == 0 and len(lineage_snps) == 0:
        #     return False
        #
        # # 0 or 1 SNP files
        # snps = self.snps.all()
        # if len(snps) == 0 or len(snps) == 1:
        #     return False
        return False

    def snps_can_be_remapped(self):
        return False

    @property
    def merging_in_progress(self):
        return False

    def remapping_in_progress(self):
        return False

    def get_discrepant_snps(self):
        try:
            return self.discrepant_snps
        except:
            return None

    def loading_snps(self):
        if self.snps.filter(setup_complete=False).count() > 0:
            return True
        else:
            return False

    def get_canonical_snps(self):
        # merge to ensure all available data is utilized
        # TODO: return merged, lineage SNPs here, for now just use SNPs with most SNPs

        # self.merge_snps()
        #
        # # use SNPs generated by lineage
        # snps = self.snps.filter(generated_by_lineage=True)
        # if len(snps) == 1:
        #     return snps[0]
        #
        # # use the one SNP file
        # snps = self.snps.all()
        # if len(snps) == 1:
        #     return snps[0]
        #
        # if len(snps) == 0:
        #     return None

        snps = self.snps.all().order_by('-snp_count')

        if snps:
            return snps[0]
        else:
            return None


    def merge_snps(self):
        if not self.snps_can_be_merged:
            return

        snps = self.snps.filter(generated_by_lineage=True)
        if len(snps) == 1:
            # remove SNPs generated by lineage since we're remaking that file
            snps[0].delete()

        if self.get_discrepant_snps():
            # remove discrepant SNPs since we'll be refreshing that data
            self.discrepant_snps.delete()

        with tempfile.TemporaryDirectory() as tmpdir:
            l = Lineage(output_dir=tmpdir)

            ind = l.create_individual('ind')
            for snps in self.snps.all():
                if snps.build != 37:
                    temp = l.create_individual('temp', snps.file.path)
                    temp.remap_snps(37)
                    temp_snps = temp.save_snps()
                    ind.load_snps(temp_snps)
                    del temp
                else:
                    ind.load_snps(snps.file.path)

                snps.merged = True
                snps.save()

            if ind.snp_count != 0:
                if len(ind.discrepant_snps) != 0:
                    dsnps = DiscrepantSnps.objects.create(user=self.user, individual=self, snp_count=len(ind.discrepant_snps))
                    discrepant_snps_file = ind.save_discrepant_snps()
                    dsnps.file.name = dsnps.get_relative_path()
                    dsnps.save()
                    shutil.move(discrepant_snps_file, dsnps.file.path)

                merged_snps_file = ind.save_snps()
                summary_info, snps_is_valid = parse_snps(merged_snps_file)

                if snps_is_valid:
                    summary_info['generated_by_lineage'] = True
                    summary_info['merged'] = True
                    self.add_snps(merged_snps_file, summary_info)

    def remap_snps(self):
        # SNPs already remapped
        if len(self.snps.filter(generated_by_lineage=True)) == 3:
            return

        if len(self.snps.filter(generated_by_lineage=True)) == 1:
            snps = self.snps.filter(generated_by_lineage=True).get()
        else:
            # TODO: merge SNPs here, but for now just get canonical SNPs; assume Build 37
            snps = self.get_canonical_snps()

        if not snps:
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            l = Lineage(output_dir=tmpdir)

            ind = l.create_individual('lineage_NCBI36', snps.file.path)
            ind.remap_snps(36)
            file = ind.save_snps()

            summary_info, snps_is_valid = parse_snps(file)

            if snps_is_valid:
                summary_info['generated_by_lineage'] = True
                summary_info['merged'] = True
                self.add_snps(file, summary_info)

            ind = l.create_individual('lineage_GRCh38', snps.file.path)
            ind.remap_snps(38)
            file = ind.save_snps()

            summary_info, snps_is_valid = parse_snps(file)

            if snps_is_valid:
                summary_info['generated_by_lineage'] = True
                summary_info['merged'] = True
                self.add_snps(file, summary_info)

    def add_snps(self, file, snps_info):
        snps = self.snps.create(user=self.user, **snps_info)

        snps.file_ext = os.path.splitext(file)[1]
        # https://stackoverflow.com/a/10906037
        snps.file.name = snps.get_relative_path()

        # make directories for SNP file
        os.makedirs(get_absolute_user_dir(self.user.uuid), exist_ok=True)

        # move file to individual's media directory
        shutil.move(file, snps.file.path)

        snps.setup_complete = True

        snps.save()


class Snps(models.Model):
    uuid = models.UUIDField(unique=True, default=uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='snps')
    individual = models.ForeignKey(Individual, on_delete=models.CASCADE, related_name='snps')
    file = models.FileField(upload_to='uploads/', storage=sendfile_storage)
    file_ext = models.CharField(max_length=16, editable=False)
    source = models.CharField(max_length=256, editable=False)
    assembly = models.CharField(max_length=8, default='GRCh37', editable=False)
    build = models.IntegerField(default=37, editable=False)
    build_detected = models.BooleanField(default=False, verbose_name="Build Detected", editable=False)
    snp_count = models.IntegerField(default=0, verbose_name="SNP Count", editable=False)
    chromosomes = models.CharField(max_length=256, editable=False)
    merged = models.BooleanField(default=False, verbose_name="Merged", editable=False)
    generated_by_lineage = models.BooleanField(default=False, editable=False)
    sex = models.CharField(default='', max_length=16, verbose_name="Determined Sex",
                           editable=False)
    uploaded_at = models.DateTimeField(auto_now_add=True, editable=False)
    # https://stackoverflow.com/a/39725317
    # https://github.com/celery/celery/issues/1813#issuecomment-33142648
    setup_task_id = models.UUIDField(unique=True, default=uuid4, editable=False)
    setup_complete = models.BooleanField(default=False, editable=False)

    def __str__(self):
        return str(self.uuid)

    def delete(self, *args, **kwargs):
        self.file.delete()

        # deleting last SNP file so remove any discrepant SNPs
        if self.individual.snps.count() == 1:
            if self.individual.get_discrepant_snps():
                self.individual.discrepant_snps.delete()

        remove_user_dir_if_empty(self.user.uuid)
        super().delete(*args, **kwargs)

    def get_relative_path(self):
        return get_relative_user_dir_file(self.user.uuid, self.uuid)

    def _get_filename_source(self):
        if self.generated_by_lineage:
            return 'lineage'
        else:
            return self.source

    def get_filename(self, include_individual_name=True):
        s = ''
        if include_individual_name:
            s += clean_string(self.individual.name) + '_'
        s += self._get_filename_source() + '_'
        s += self.assembly + self.file_ext
        return s

    def get_url(self):
        return reverse('download_snps', args=[self.uuid])

    def setup(self, progress_recorder=None):
        summary_info, snps_is_valid = parse_snps(self.file.path)
        if snps_is_valid:
            Snps.objects.filter(id=self.id).update(**summary_info)
            self.refresh_from_db()
            os.makedirs(get_absolute_user_dir(self.user.uuid), exist_ok=True)
            original_path = self.file.path
            if '.zip' in original_path:
                self.file_ext = '.zip'
            elif '.csv.gz' in original_path:
                self.file_ext = '.csv.gz'
            elif '.gz' in original_path:
                self.file_ext = '.gz'
            elif '.csv' in original_path:
                self.file_ext = '.csv'
            else:
                self.file_ext = '.txt'
            self.file.name = self.get_relative_path()
            shutil.move(original_path, self.file.path)
            self.setup_complete = True
            self.save()
        else:
            self.delete()

class DiscrepantSnps(models.Model):
    uuid = models.UUIDField(unique=True, default=uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='discrepant_snps')
    individual = models.OneToOneField(Individual, on_delete=models.CASCADE, related_name='discrepant_snps')
    file = models.FileField(storage=sendfile_storage, editable=False)
    snp_count = models.IntegerField(default=0, verbose_name="Discrepant SNPs", editable=False)

    def __str__(self):
        return str(self.uuid)

    def delete(self, *args, **kwargs):
        self.file.delete()
        remove_user_dir_if_empty(self.user.uuid)
        super().delete(*args, **kwargs)

    def get_relative_path(self):
        return get_relative_user_dir_file(self.user.uuid, self.uuid)

    def get_filename(self, include_individual_name=True):
        s = ''
        if include_individual_name:
            s += clean_string(self.individual.name) + '_'
        s += 'lineage_discrepant_snps.csv'
        return  s

    def get_url(self):
        return reverse('download_discrepant_snps', args=[self.individual.uuid])

class SharedDnaGenes(models.Model):
    uuid = models.UUIDField(unique=True, default=uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='shared_dna_genes')
    individual1 = models.ForeignKey(Individual, on_delete=models.CASCADE,
                                    related_name='shared_dna_genes_ind1', verbose_name='1st '
                                                                                       'Individual')
    individual2 = models.ForeignKey(Individual, on_delete=models.CASCADE,
                                    related_name='shared_dna_genes_ind2', verbose_name='2nd '
                                                                                       'Individual')
    # https://stackoverflow.com/a/12384584
    # https://stackoverflow.com/a/9111694
    cM_threshold = models.DecimalField(default=0.75, max_digits=5, decimal_places=2,
                                       validators=[MinValueValidator(0)],
                                       verbose_name=" cM Threshold")
    snp_threshold = models.PositiveIntegerField(default=1000, verbose_name="SNP Threshold")

    # files with results
    shared_dna_plot_png = models.FileField(storage=sendfile_storage, editable=False)
    shared_dna_one_chrom_csv = models.FileField(storage=sendfile_storage, editable=False)
    shared_dna_one_chrom_pickle = models.FileField(storage=sendfile_storage, editable=False)
    shared_dna_two_chrom_csv = models.FileField(storage=sendfile_storage, editable=False)
    shared_dna_two_chrom_pickle = models.FileField(storage=sendfile_storage, editable=False)
    shared_genes_one_chrom_csv = models.FileField(storage=sendfile_storage, editable=False)
    shared_genes_one_chrom_pickle = models.FileField(storage=sendfile_storage, editable=False)
    shared_genes_two_chrom_csv = models.FileField(storage=sendfile_storage, editable=False)
    shared_genes_two_chrom_pickle = models.FileField(storage=sendfile_storage, editable=False)

    # summary statistics
    total_shared_segments_one_chrom = models.PositiveIntegerField(default=0, editable=False)
    total_shared_segments_two_chrom = models.PositiveIntegerField(default=0, editable=False)
    total_shared_cMs_one_chrom = models.DecimalField(default=0, max_digits=6, decimal_places=2,
                                                     editable=False, verbose_name=' cMs Shared '
                                                                                  'DNA '
                                                                                  '(1 chrom)')
    total_shared_cMs_two_chrom = models.DecimalField(default=0, max_digits=6, decimal_places=2,
                                                     editable=False, verbose_name=' cMs Shared '
                                                                                  'DNA (2 chrom)')
    total_snps_one_chrom = models.PositiveIntegerField(default=0, editable=False)
    total_snps_two_chrom = models.PositiveIntegerField(default=0, editable=False)
    total_chrom_one_chrom = models.PositiveIntegerField(default=0, editable=False)
    total_chrom_two_chrom = models.PositiveIntegerField(default=0, editable=False)
    total_shared_genes_one_chrom = models.PositiveIntegerField(default=0, editable=False,
                                                               verbose_name='Shared Genes (1 '
                                                                            'chrom)')
    total_shared_genes_two_chrom = models.PositiveIntegerField(default=0, editable=False,
                                                               verbose_name='Shared Genes (2 '
                                                                            'chrom)')

    setup_complete = models.BooleanField(default=False, editable=False)
    setup_task_id = models.UUIDField(unique=True, default=uuid4, editable=False)

    def __str__(self):
        return str(self.uuid)

    # https://stackoverflow.com/a/26546181
    @receiver(pre_delete, sender=Individual)
    def pre_delete_individual(sender, instance, **kwargs):
        for shared_dna_genes in instance.shared_dna_genes_ind1.all():
            shared_dna_genes.delete()

        for shared_dna_genes in instance.shared_dna_genes_ind2.all():
            shared_dna_genes.delete()

    def delete(self, *args, **kwargs):
        for field in self._meta.get_fields():
            # https://stackoverflow.com/a/3106314
            if isinstance(field, models.FileField):
                # https://stackoverflow.com/a/9379402
                getattr(self, field.name).delete()

        remove_user_dir_if_empty(self.user.uuid)
        super().delete(*args, **kwargs)

    def get_shared_dna_one_chrom(self):
        df = pd.read_pickle(self.shared_dna_one_chrom_pickle.path)
        df['segment_col'] = df.index
        return df.to_dict('records')

    def get_shared_dna_two_chrom(self):
        df = pd.read_pickle(self.shared_dna_two_chrom_pickle.path)
        df['segment_col'] = df.index
        return df.to_dict('records')

    def get_shared_dna_plot_png_url(self):
        return reverse('shared_dna_plot', args=[self.uuid])

    def get_shared_dna_one_chrom_csv_url(self):
        return reverse('shared_dna_one_chrom', args=[self.uuid])

    def get_shared_dna_two_chrom_csv_url(self):
        return reverse('shared_dna_two_chrom', args=[self.uuid])

    def get_shared_genes_one_chrom_csv_url(self):
        return reverse('shared_genes_one_chrom', args=[self.uuid])

    def get_shared_genes_two_chrom_csv_url(self):
        return reverse('shared_genes_two_chrom', args=[self.uuid])

    def _get_individuals_str(self):
        s = ''
        s += clean_string(self.individual1.name) + '_'
        s += clean_string(self.individual2.name) + '_'
        return s

    def _get_threshold_str(self):
        return 'cMs' + clean_string(str(self.cM_threshold)) + '_SNPs' + \
               clean_string(str(self.snp_threshold)) + '_'

    def get_shared_dna_one_chrom_csv_filename(self, include_individual_names=True):
        s = ''
        if include_individual_names:
            s += self._get_individuals_str()
        s += 'shared_dna_one_chrom_GRCh37_'
        s += self._get_threshold_str()
        s += '.csv.gz'
        return s

    def get_shared_dna_two_chrom_csv_filename(self, include_individual_names=True):
        s = ''
        if include_individual_names:
            s += self._get_individuals_str()
        s += 'shared_dna_two_chrom_GRCh37_'
        s += self._get_threshold_str()
        s += '.csv.gz'
        return s

    def get_shared_genes_one_chrom_csv_filename(self, include_individual_names=True):
        s = ''
        if include_individual_names:
            s += self._get_individuals_str()
        s += 'shared_genes_one_chrom_GRCh37_'
        s += self._get_threshold_str()
        s += '.csv.gz'
        return s

    def get_shared_genes_two_chrom_csv_filename(self, include_individual_names=True):
        s = ''
        if include_individual_names:
            s += self._get_individuals_str()
        s += 'shared_genes_two_chrom_GRCh37_'
        s += self._get_threshold_str()
        s += '.csv.gz'
        return s

    def find_shared_dna_genes(self, progress_recorder=None):
        ind1_snps = self.individual1.get_canonical_snps()
        ind2_snps = self.individual2.get_canonical_snps()

        with tempfile.TemporaryDirectory() as tmpdir:
            l = Lineage(output_dir=tmpdir)

            ind1_snps_file = shutil.copy(ind1_snps.file.path, os.path.join(tmpdir, 'ind1_snps' +
                                                                  ind1_snps.file_ext))

            ind2_snps_file = shutil.copy(ind2_snps.file.path, os.path.join(tmpdir, 'ind2_snps' +
                                                                  ind2_snps.file_ext))

            ind1 = l.create_individual(self.individual1.name, ind1_snps_file)
            ind2 = l.create_individual(self.individual2.name, ind2_snps_file)

            shared_dna_one_chrom, shared_dna_two_chrom, shared_genes_one_chrom, shared_genes_two_chrom = \
                l.find_shared_dna(ind1, ind2, cM_threshold=float(self.cM_threshold),
                                  snp_threshold=int(self.snp_threshold), shared_genes=True,
                                  save_output=True)

            self.total_shared_segments_one_chrom = len(shared_dna_one_chrom)
            self.total_shared_segments_two_chrom = len(shared_dna_two_chrom)
            self.total_shared_cMs_one_chrom = Decimal(shared_dna_one_chrom['cMs'].sum())
            self.total_shared_cMs_two_chrom = Decimal(shared_dna_two_chrom['cMs'].sum())
            self.total_snps_one_chrom = shared_dna_one_chrom['snps'].sum()
            self.total_snps_two_chrom = shared_dna_two_chrom['snps'].sum()
            self.total_chrom_one_chrom = len(shared_dna_one_chrom['chrom'].unique())
            self.total_chrom_two_chrom = len(shared_dna_two_chrom['chrom'].unique())
            self.total_shared_genes_one_chrom = len(shared_genes_one_chrom)
            self.total_shared_genes_two_chrom = len(shared_genes_two_chrom)

            for root, dirs, files in os.walk(tmpdir):
                for file in files:
                    file_path = os.path.join(root, file)
                    if '.png' in file:
                        self.shared_dna_plot_png.name = \
                            get_relative_user_dir_file(self.user.uuid, uuid4(), '.png')
                        shutil.move(file_path, self.shared_dna_plot_png.path)

                    elif 'shared_dna_one_chrom' in file:
                        self.shared_dna_one_chrom_csv = \
                            get_relative_user_dir_file(self.user.uuid, uuid4())
                        compress_file(file_path, self.shared_dna_one_chrom_csv.path)

                        self.shared_dna_one_chrom_pickle = \
                            get_relative_user_dir_file(self.user.uuid, uuid4(), '.pkl.gz')

                        shared_dna_one_chrom.to_pickle(self.shared_dna_one_chrom_pickle.path)

                    elif 'shared_genes_one_chrom' in file:
                        self.shared_genes_one_chrom_csv = \
                            get_relative_user_dir_file(self.user.uuid, uuid4())
                        compress_file(file_path, self.shared_genes_one_chrom_csv.path)

                        self.shared_genes_one_chrom_pickle = \
                            get_relative_user_dir_file(self.user.uuid, uuid4(), '.pkl.gz')

                        shared_genes_one_chrom.to_pickle(self.shared_genes_one_chrom_pickle.path)

                    elif 'shared_dna_two_chrom' in file:
                        self.shared_dna_two_chrom_csv = \
                            get_relative_user_dir_file(self.user.uuid, uuid4())
                        compress_file(file_path, self.shared_dna_two_chrom_csv.path)

                        self.shared_dna_two_chrom_pickle = \
                            get_relative_user_dir_file(self.user.uuid, uuid4(), '.pkl.gz')

                        shared_dna_two_chrom.to_pickle(self.shared_dna_two_chrom_pickle.path)

                    elif 'shared_genes_two_chrom' in file:
                        self.shared_genes_two_chrom_csv = \
                            get_relative_user_dir_file(self.user.uuid, uuid4())
                        compress_file(file_path, self.shared_genes_two_chrom_csv.path)

                        self.shared_genes_two_chrom_pickle = \
                            get_relative_user_dir_file(self.user.uuid, uuid4(), '.pkl.gz')

                        shared_genes_two_chrom.to_pickle(self.shared_genes_two_chrom_pickle.path)

        self.setup_complete = True
        self.save()


class DiscordantSnps(models.Model):
    uuid = models.UUIDField(unique=True, default=uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='discordant_snps')
    individual1 = models.ForeignKey(Individual, on_delete=models.CASCADE,
                                    related_name='discordant_snps_ind1', verbose_name='1st '
                                                                                       'Individual')
    individual2 = models.ForeignKey(Individual, on_delete=models.CASCADE,
                                    related_name='discordant_snps_ind2', verbose_name='2nd '
                                                                                       'Individual')
    # https://stackoverflow.com/a/6620137
    individual3 = models.ForeignKey(Individual, on_delete=models.CASCADE,
                                    related_name='discordant_snps_ind3', verbose_name='3rd '
                                                                                       'Individual', blank=True, null=True)

    discordant_snps_csv = models.FileField(storage=sendfile_storage, editable=False)
    discordant_snps_pickle = models.FileField(storage=sendfile_storage, editable=False)

    total_discordant_snps = models.PositiveIntegerField(default=0, editable=False)

    setup_complete = models.BooleanField(default=False, editable=False)
    setup_task_id = models.UUIDField(unique=True, default=uuid4, editable=False)


    def __str__(self):
        return str(self.uuid)

    # https://stackoverflow.com/a/26546181
    @receiver(pre_delete, sender=Individual)
    def pre_delete_individual(sender, instance, **kwargs):
        for discordant_snps in instance.discordant_snps_ind1.all():
            discordant_snps.delete()

        for discordant_snps in instance.discordant_snps_ind2.all():
            discordant_snps.delete()

        for discordant_snps in instance.discordant_snps_ind3.all():
            discordant_snps.delete()

    def delete(self, *args, **kwargs):
        for field in self._meta.get_fields():
            # https://stackoverflow.com/a/3106314
            if isinstance(field, models.FileField):
                # https://stackoverflow.com/a/9379402
                getattr(self, field.name).delete()

        remove_user_dir_if_empty(self.user.uuid)
        super().delete(*args, **kwargs)

    def get_discordant_snps_csv_url(self):
        return reverse('download_discordant_snps', args=[self.uuid])

    def _get_individuals_str(self):
        s = ''
        s += clean_string(self.individual1.name) + '_'
        s += clean_string(self.individual2.name) + '_'

        if self.individual3:
            s += clean_string(self.individual3.name) + '_'

        return s

    def get_discordant_snps_csv_filename(self, include_individual_names=True):
        s = ''
        if include_individual_names:
            s += self._get_individuals_str()
        s += 'discordant_snps_GRCh37.csv.gz'
        return s

    def find_discordant_snps(self, progress_recorder=None):
        ind1_snps = self.individual1.get_canonical_snps()
        ind2_snps = self.individual2.get_canonical_snps()

        if self.individual3:
            ind3_snps = self.individual3.get_canonical_snps()

        with tempfile.TemporaryDirectory() as tmpdir:
            l = Lineage(output_dir=tmpdir)

            ind1_snps_file = shutil.copy(ind1_snps.file.path, os.path.join(tmpdir, 'ind1_snps' +
                                                                              ind1_snps.file_ext))

            ind2_snps_file = shutil.copy(ind2_snps.file.path, os.path.join(tmpdir, 'ind2_snps' +
                                                                              ind2_snps.file_ext))

            if self.individual3:
                ind3_snps_file = shutil.copy(ind3_snps.file.path, os.path.join(tmpdir,
                                                                               'ind3_snps' +
                                                                               ind3_snps.file_ext))


            ind1 = l.create_individual(self.individual1.name, ind1_snps_file)
            ind2 = l.create_individual(self.individual2.name, ind2_snps_file)

            if self.individual3:
                ind3 = l.create_individual(self.individual3.name, ind3_snps_file)
            else:
                ind3 = None

            discordant_snps = l.find_discordant_snps(ind1, ind2, ind3, save_output=True)

            self.total_discordant_snps = len(discordant_snps)

            for root, dirs, files in os.walk(tmpdir):
                for file in files:
                    file_path = os.path.join(root, file)
                    if 'discordant_snps' in file:
                        self.discordant_snps_csv.name = \
                            get_relative_user_dir_file(self.user.uuid, uuid4())
                        compress_file(file_path, self.discordant_snps_csv.path)

                        self.discordant_snps_pickle = \
                            get_relative_user_dir_file(self.user.uuid, uuid4(), '.pkl.gz')
                        discordant_snps.to_pickle(self.discordant_snps_pickle.path)

                        break

        self.setup_complete = True
        self.save()
