from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from OpenBench.fleet.housekeeping import ABANDONED_AFTER, EXITED_AFTER, apply_prune, plan_prune
from OpenBench.models import Machine


class Command(BaseCommand):
    help = 'Report, and with --apply delete, Machine registrations that never played a game'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--apply', action='store_true', help='Delete the registrations; without it nothing changes')
        parser.add_argument('--dry-run', action='store_true', help='Only report; the default')
        parser.add_argument(
            '--days',
            type=int,
            default=ABANDONED_AFTER.days,
            help='Age after which a never-used registration of a Client that keeps polling is removed',
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options['apply'] and options['dry_run']:
            raise CommandError('Choose one of --apply and --dry-run')
        if options['days'] < 1:
            raise CommandError('--days must be at least 1')

        plan = plan_prune(timezone.now(), timedelta(days=options['days']))
        hosts = Machine.objects.values('host_key').distinct().count()
        minutes = EXITED_AFTER.seconds // 60
        self.stdout.write(
            f'{plan.registrations} registrations of {hosts} machines; {plan.in_use} have Results and are kept.\n'
            f'{len(plan.exited)} never used, exited (--single-workload or --fleet), idle over {minutes} minutes.\n'
            f'{len(plan.abandoned)} never used, idle over {options["days"]} days.'
        )

        if not options['apply']:
            self.stdout.write(f'Dry run: {len(plan.prunable)} registrations would be deleted. Pass --apply to delete.')
            return

        self.stdout.write(f'Deleted {apply_prune(plan)} registrations.')
