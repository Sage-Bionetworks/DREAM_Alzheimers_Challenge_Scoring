import synapseclient
import synapseclient.utils as utils
from synapseclient.exceptions import *
from synapseclient import Activity
from synapseclient import Project, Folder, File
from synapseclient import Evaluation, Submission, SubmissionStatus
from synapseclient import Wiki

from datetime import datetime, timedelta
from itertools import izip
from StringIO import StringIO

from ad_challenge_scoring import *

import lock
import argparse
import json
import math
import os
import random
import sys
import time
import traceback
import urllib
import uuid


# how many submissions will be updated in a single batch
BATCH_SIZE = 100

# how many times to we retry batch uploads of submission annotations
BATCH_UPLOAD_RETRY_COUNT = 7

ADMIN_USER_IDS = [1421212]

QUOTA = 100
SEND_VALIDATION_SUCCESS = False


syn = synapseclient.Synapse()



## read in email templates
with open("templates/confirmation_email.txt") as f:
    confirmation_template = f.read()

with open("templates/validation_error_email.txt") as f:
    validation_error_template = f.read()

with open("templates/scored_email.txt") as f:
    scored_template = f.read()

with open("templates/scoring_error_email.txt") as f:
    scoring_error_template = f.read()

with open("templates/error_notification_email.txt") as f:
    error_notification_template = f.read()


def update_submissions_status_batch(evaluation, statuses):
    for retry in range(BATCH_UPLOAD_RETRY_COUNT):
        try:
            token = None
            offset = 0
            while offset < len(statuses):
                batch = {"statuses"     : statuses[offset:offset+BATCH_SIZE],
                         "isFirstBatch" : (offset==0),
                         "isLastBatch"  : (offset+BATCH_SIZE>=len(statuses)),
                         "batchToken"   : token}
                response = syn.restPUT("/evaluation/%s/statusBatch" % evaluation.id, json.dumps(batch))
                token = response.get('nextUploadToken', None)
                offset += BATCH_SIZE
            ## finished batch uploading successfully
            break
        except SynapseHTTPError as err:
            # on 412 ConflictingUpdateException we want to retry
            if err.response.status_code == 412:
                sys.stderr.write('%s, retrying...\n' % err.message)
                time.sleep(2)
            else:
                raise


def send_message(template, submission, status, evaluation, message):
    profile = syn.getUserProfile(submission.userId)

    #print "sending message to %s" % submission.userId

    ## fill in the template
    message_body = template.format(
        username=profile.get('firstName', profile.get('userName', profile['ownerId'])),
        submission_id=submission.id,
        submission_name=submission.name,
        evaluation_id=evaluation.id,
        evaluation_name=evaluation.name,
        team=submission.get('submitterAlias', 'no team specified'),
        message=message)

    return syn.sendMessage(
        userIds=[submission.userId],
        messageSubject="Submission to %s, %s" % (evaluation.name, status),
        messageBody=message_body)


def validate_submission(submission, status):
    status.status = "VALIDATED" 
    return status, "OK"


def validate(evaluation, validation_func=validate_submission, send_messages=False, dry_run=False):
    """
    It may be convenient to validate submissions in one pass before scoring
    them, especially if scoring takes a long time.
    """
    sys.stdout.write('\n\n' + '-' * 60 + '\n')
    sys.stdout.write('validating evaluation: %s %s\n' % (evaluation.id, evaluation.name))
    sys.stdout.flush()

    submission_counts_by_user = count_submissions_by_user(evaluation, status='SCORED')

    count = 0

    for submission, status in syn.getSubmissionBundles(evaluation, status='RECEIVED'):

        sys.stdout.write('\nvalidating submission: %s %s\n' % (submission.id, submission.name))
        sys.stdout.flush()

        count += 1

        if submission_counts_by_user.get(submission.userId, 0) >= QUOTA:
            status.status = "INVALID"
            validation_message = "You have reached the submission quota. You have submitted %d entries out of a maximum of %d allowed." % (submission_counts_by_user.get(submission.userId, 0), QUOTA)

        else:
            ## keep track of user's submission counts as we go
            submission_counts_by_user.setdefault(submission.userId, 0)
            submission_counts_by_user[submission.userId] += 1

            ## refetch the submission so that we get the file path
            ## to be later replaced by a "downloadFiles" flag on getSubmissionBundles
            submission = syn.getSubmission(submission)

            try:
                status, validation_message = validation_func(submission, status)
            except Exception as ex1:
                sys.stderr.write('Error validating submission %s %s:\n' % (submission.name, submission.id))
                st = StringIO()
                traceback.print_exc(file=st)
                sys.stderr.write(st.getvalue())
                sys.stderr.write('\n')
                status.status = "INVALID"
                validation_message = st.getvalue()

        if not dry_run:
            syn.store(status)

        ## send message AFTER storing status to ensure we don't get repeat messages
        if send_messages and (status.status=="INVALID" or SEND_VALIDATION_SUCCESS):
            template = confirmation_template if status.status=="VALIDATED" else validation_error_template
            response = send_message(template, submission, status.status, evaluation, validation_message)
            print "sent message: ", response

        print submission.id, submission.name, submission.userId, status.status

    print "\nvalidated %d submissions." % count
    print '-' * 60 + '\n'


def score_submission(submission, status):
    status.status = "SCORED"
    return status, "OK"


def score(evaluation, scoring_func=score_submission, send_messages=False, dry_run=False):

    sys.stdout.write('\n\n' + '-' * 60 + '\n')
    sys.stdout.write('scoring evaluation: %s %s\n' % (evaluation.id, evaluation.name))
    sys.stdout.flush()

    ## collect statuses here for batch update
    statuses = []
    submissions = []
    messages = []

    submission_counts_by_user = count_submissions_by_user(evaluation, status='SCORED')

    for submission, status in syn.getSubmissionBundles(evaluation, status='VALIDATED'):

        sys.stdout.write('\nscoring submission: %s %s\n' % (submission.id, submission.name))
        sys.stdout.flush()

        ## refetch the submission so that we get the file path
        ## to be later replaced by a "downloadFiles" flag on getSubmissionBundles
        submission = syn.getSubmission(submission)

        try:
            status, msg = scoring_func(submission, status)

            ## keep track of user's submission counts as we go
            submission_counts_by_user.setdefault(submission.userId, 0)
            submission_counts_by_user[submission.userId] += 1

            annotations = synapseclient.annotations.from_synapse_annotations(status.annotations)
            annotations['submission_number'] = submission_counts_by_user[submission.userId]
            status.annotations = synapseclient.annotations.to_submission_status_annotations(annotations, is_private=False)

            msg += "\nThis is your %s submission out of a maximum of %d allowed." % (
                    to_ordinal(submission_counts_by_user[submission.userId]), QUOTA)
            messages.append(msg)
        except Exception as ex1:
            sys.stderr.write('Error scoring submission %s %s:\n' % (submission.name, submission.id))
            st = StringIO()
            traceback.print_exc(file=st)
            sys.stderr.write(st.getvalue())
            sys.stderr.write('\n')
            status.status = "INVALID"
            messages.append(st.getvalue())

        ## we could store each status update individually, but in this example
        ## we collect the updated status objects to do a batch update.
        #status = syn.store(status)
        statuses.append(status)
        submissions.append(submission)

        print submission.id, submission.name, submission.userId, status.status

    ## Update statuses in batch. This can be much faster than individual updates,
    ## especially in rank based scoring methods which recalculate scores for all
    ## submissions each time a new submission is received.
    if not dry_run:
        update_submissions_status_batch(evaluation, statuses)

    if send_messages:
        for submission, status, message in izip(submissions, statuses, messages):
            template = scored_template if status.status=="SCORED" else scoring_error_template
            response = send_message(template, submission, status.status, evaluation, message)
            print "sent message: ", response

    print "\nscored %d submissions." % len(submissions)
    print '-' * 60 + '\n'


def list_submissions(evaluation, status=None, **kwargs):
    print '\n\nSubmissions for: %s %s' % (evaluation.id, evaluation.name)
    print '-' * 60

    for submission, status in syn.getSubmissionBundles(evaluation, status=status):
        print submission.id, submission.name, submission.userId, status.status


def count_submissions_by_user(evaluation, status=None):
    submission_counts_by_user = {}
    for submission, status in syn.getSubmissionBundles(evaluation, status=status):
        submission_counts_by_user.setdefault(submission.userId, 0)
        submission_counts_by_user[submission.userId] += 1
    return submission_counts_by_user


def to_ordinal(i):
    ## teens are all 1Xth
    if i % 100 >= 11 and i % 100 < 20:
        return "%dth" % i
    elif i % 10 == 1:
        return "%dst" % i
    elif i % 10 == 2:
        return "%dnd" % i
    elif i % 10 == 3:
        return "%drd" % i
    else:
        return "%dth" % i


def command_list(args):
    list_submissions(evaluation=syn.getEvaluation(args.evaluation),
                     status=args.status)


def command_validate(args):
    validate(evaluation=syn.getEvaluation(args.evaluation),
             validation_func=globals()[challenge_evaluations_map[int(args.evaluation)]['validation_function']],
             send_messages=args.send_messages,
             dry_run=args.dry_run)


def command_score(args):
    score(evaluation=syn.getEvaluation(args.evaluation),
          scoring_func=globals()[challenge_evaluations_map[int(args.evaluation)]['scoring_function']],
          send_messages=args.send_messages,
          dry_run=args.dry_run)


def command_check_status(args):
    submission = syn.getSubmission(args.submission)
    status = syn.getSubmissionStatus(args.submission)
    evaluation = syn.getEvaluation(submission.evaluationId)
    ## deleting the entity key is a hack to work around a bug which prevents
    ## us from printing a submission
    del submission['entity']
    print evaluation
    print submission
    print status


def command_reset(args):
    status = syn.getSubmissionStatus(args.submission)
    status.status = args.status
    if not args.dry_run:
        print syn.store(status)


def command_score_challenge(args):
    for challenge_evaluation in challenge_evaluations:
        evaluation = syn.getEvaluation(challenge_evaluation['id'])

        validation_function = globals()[challenge_evaluation['validation_function']]
        validate(evaluation, validation_function,
            send_messages=args.send_messages,
            dry_run=args.dry_run)

        scoring_function = globals()[challenge_evaluation['scoring_function']]
        score(evaluation, scoring_function,
            send_messages=args.send_messages,
            dry_run=args.dry_run)


def challenge():

    parser = argparse.ArgumentParser()

    parser.add_argument("-u", "--user", help="UserName", default=None)
    parser.add_argument("-p", "--password", help="Password", default=None)
    parser.add_argument("--notifications", help="Send error notifications to challenge admins", action="store_true", default=False)
    parser.add_argument("--send-messages", action="store_true", default=False)
    parser.add_argument("--dry-run", help="Perform the requested command without updating anything in Synapse", action="store_true", default=False)

    subparsers = parser.add_subparsers(title="subcommand")

    parser_list = subparsers.add_parser('list', help="List submissions to an evaluation")
    parser_list.add_argument("evaluation", metavar="EVALUATION-ID", default=None)
    parser_list.add_argument("-s", "--status", default=None)
    parser_list.set_defaults(func=command_list)

    parser_validate = subparsers.add_parser('validate', help="Validate all RECEIVED submissions to an evaluation")
    parser_validate.add_argument("evaluation", metavar="EVALUATION-ID", default=None)
    parser_validate.set_defaults(func=command_validate)

    parser_score = subparsers.add_parser('score', help="Score all VALIDATED submissions to an evaluation")
    parser_score.add_argument("evaluation", metavar="EVALUATION-ID", default=None)
    parser_score.set_defaults(func=command_score)

    parser_status = subparsers.add_parser('status', help="Check the status of a submission")
    parser_status.add_argument("submission")
    parser_status.set_defaults(func=command_check_status)

    parser_reset = subparsers.add_parser('reset', help="Reset a submission to RECEIVED for re-scoring (or set to some other status)")
    parser_reset.add_argument("submission")
    parser_reset.add_argument("-s", "--status", default='RECEIVED')
    parser_reset.set_defaults(func=command_reset)

    parser_score_challenge = subparsers.add_parser('score-challenge', help="Validate and score submissions to all evaluations in a challenge")
    parser_score_challenge.set_defaults(func=command_score_challenge)
 
    args = parser.parse_args()

    print "\n" * 2, "-" * 60
    print datetime.utcnow().isoformat()

    ## Acquire lock, don't run two scoring scripts at once
    try:
        update_lock = lock.acquire_lock_or_fail('challenge', max_age=timedelta(hours=4))
    except lock.LockedException:
        print u"Is the scoring script already running? Can't acquire lock."
        # can't acquire lock, so return error code 75 which is a
        # temporary error according to /usr/include/sysexits.h
        return 75

    try:
        if not args.user:
            args.user = os.environ.get('SYNAPSE_USER', None)
        if not args.password:
            args.password = os.environ.get('SYNAPSE_PASSWORD', None)
        syn.login(email=args.user, password=args.password)
        args.func(args)

    except Exception as ex1:
        sys.stderr.write('Error in scoring script:\n')
        st = StringIO()
        traceback.print_exc(file=st)
        sys.stderr.write(st.getvalue())
        sys.stderr.write('\n')
        message = error_notification_template.format(message=st.getvalue())

        if args.notifications:
            response = syn.sendMessage(
                userIds=ADMIN_USER_IDS,
                messageSubject="Exception in AD Challenge scoring harness",
                messageBody=message)
            print "sent message: ", response

    finally:
        update_lock.release()

    print "\ndone: ", datetime.utcnow().isoformat()
    print "-" * 60, "\n" * 2


if __name__ == '__main__':
    challenge()