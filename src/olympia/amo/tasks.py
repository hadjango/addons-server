import datetime

from django.core.mail import EmailMessage, EmailMultiAlternatives

import commonware.log
import phpserialize

from olympia import amo
from olympia.amo.celery import task
from olympia.amo.utils import get_email_backend
from olympia.bandwagon.models import Collection
from olympia.devhub.models import ActivityLog
from olympia.editors.models import EventLog
from olympia.reviews.models import Review
from olympia.stats.models import Contribution


log = commonware.log.getLogger('z.task')


@task
def send_email(recipient, subject, message, from_email=None,
               html_message=None, attachments=None, real_email=False,
               cc=None, headers=None, fail_silently=False, async=False,
               max_retries=None, **kwargs):
    backend = EmailMultiAlternatives if html_message else EmailMessage
    connection = get_email_backend(real_email)
    result = backend(subject, message,
                     from_email, recipient, cc=cc, connection=connection,
                     headers=headers, attachments=attachments)
    if html_message:
        result.attach_alternative(html_message, 'text/html')
    try:
        result.send(fail_silently=False)
        return True
    except Exception as e:
        log.error('send_mail failed with error: %s' % e)
        if async:
            return send_email.retry(exc=e, max_retries=max_retries)
        elif not fail_silently:
            raise
        else:
            return False


@task
def set_modified_on_object(obj, **kw):
    """Sets modified on one object at a time."""
    try:
        log.info('Setting modified on object: %s, %s' %
                 (obj.__class__.__name__, obj.pk))
        obj.update(modified=datetime.datetime.now())
    except Exception, e:
        log.error('Failed to set modified on: %s, %s - %s' %
                  (obj.__class__.__name__, obj.pk, e))


@task
def delete_logs(items, **kw):
    log.info('[%s@%s] Deleting logs' % (len(items), delete_logs.rate_limit))
    ActivityLog.objects.filter(pk__in=items).exclude(
        action__in=amo.LOG_KEEP).delete()


@task
def delete_stale_contributions(items, **kw):
    log.info('[%s@%s] Deleting stale contributions' %
             (len(items), delete_stale_contributions.rate_limit))
    Contribution.objects.filter(
        transaction_id__isnull=True, pk__in=items).delete()


@task
def delete_anonymous_collections(items, **kw):
    log.info('[%s@%s] Deleting anonymous collections' %
             (len(items), delete_anonymous_collections.rate_limit))
    Collection.objects.filter(type=amo.COLLECTION_ANONYMOUS,
                              pk__in=items).delete()


@task
def migrate_editor_eventlog(items, **kw):
    log.info('[%s@%s] Migrating eventlog items' %
             (len(items), migrate_editor_eventlog.rate_limit))
    for item in EventLog.objects.filter(pk__in=items):
        kw = dict(user=item.user, created=item.created)
        if item.action == 'review_delete':
            details = None
            try:
                details = phpserialize.loads(item.notes)
            except ValueError:
                pass
            amo.log(amo.LOG.DELETE_REVIEW, item.changed_id, details=details,
                    **kw)
        elif item.action == 'review_approve':
            try:
                r = Review.objects.get(pk=item.changed_id)
                amo.log(amo.LOG.ADD_REVIEW, r, r.addon, **kw)
            except Review.DoesNotExist:
                log.warning("Couldn't find review for %d" % item.changed_id)
