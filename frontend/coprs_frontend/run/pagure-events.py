#!/usr/bin/python3

import json
import pprint
import zmq
import sys
import os
import logging
import requests
import re
import munch

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
)

from coprs import db, app, models
from coprs.logic.coprs_logic import CoprsLogic, CoprDirsLogic
from coprs.logic.builds_logic import BuildsLogic
from coprs.logic.complex_logic import ComplexLogic
from coprs.logic.packages_logic import PackagesLogic
from coprs import helpers

from urllib.parse import urlparse

SCM_SOURCE_TYPE = helpers.BuildSourceEnum("scm")

logging.basicConfig(
    filename='{0}/build_on_pagure_commit.log'.format(app.config.get('LOG_DIR')),
    format='[%(asctime)s][%(levelname)6s]: %(message)s',
    level=logging.DEBUG)

log = logging.getLogger(__name__)
log.addHandler(logging.StreamHandler(sys.stdout))

if os.getenv('PAGURE_EVENTS_TESTONLY'):
    ENDPOINT = 'tcp://stg.pagure.io:9940'
else:
    ENDPOINT = 'tcp://hub.fedoraproject.org:9940'

log.info("ENDPOINT = {}".format(ENDPOINT))

TOPICS = {
    'io.pagure.prod.pagure.git.receive': 'https://pagure.io/',
    'io.pagure.prod.pagure.pull-request.new': 'https://pagure.io/',
    'io.pagure.prod.pagure.pull-request.comment.added': 'https://pagure.io/',
    'org.fedoraproject.prod.pagure.git.receive': 'https://src.fedoraproject.org/',
    'org.fedoraproject.prod.pagure.pull-request.new': 'https://src.fedoraproject.org/',
    'org.fedoraproject.prod.pagure.pull-request.comment.added': 'https://src.fedoraproject.org/',
    'io.pagure.stg.pagure.git.receive': 'https://stg.pagure.io/', # testing only
    'io.pagure.stg.pagure.pull-request.new': 'https://stg.pagure.io/', # testing only
    'io.pagure.stg.pagure.pull-request.comment.added': 'https://stg.pagure.io/', # testing only
}


class ScmPackage(object):
    def __init__(self, db_row):
        self.pkg_id = db_row.package_id
        self.copr_id = db_row.copr_id

        self.source_json_dict = json.loads(db_row.source_json)
        self.clone_url = self.source_json_dict.get('clone_url') or ''
        self.committish = self.source_json_dict.get('committish') or ''
        self.subdirectory = self.source_json_dict.get('subdirectory') or ''

        self.package = ComplexLogic.get_package_by_id_safe(self.pkg_id)
        self.copr = ComplexLogic.get_copr_by_id_safe(self.copr_id)

    def build(self, source_dict_update, copr_dir, update_callback,
              scm_object_type, scm_object_id, scm_object_url):

        if self.package.copr_dir.name != copr_dir.name:
            package = PackagesLogic.get_or_create(copr_dir, self.package.name, self.package)
        else:
            package = self.package

        build = BuildsLogic.rebuild_package(
            package, source_dict_update, copr_dir, update_callback,
            scm_object_type, scm_object_id, scm_object_url)

        return build

    @classmethod
    def get_candidates_for_rebuild(cls, clone_url):
        if db.engine.url.drivername == 'sqlite':
            placeholder = '?'
            true = '1'
        else:
            placeholder = '%s'
            true = 'true'

        rows = db.engine.execute(
            """
            SELECT package.id AS package_id, package.source_json AS source_json, package.copr_id AS copr_id
            FROM package JOIN copr_dir ON package.copr_dir_id = copr_dir.id
            WHERE package.source_type = {0} AND
                  package.webhook_rebuild = {1} AND
                  copr_dir.main = {2} AND
                  package.source_json ILIKE {placeholder}
            """.format(SCM_SOURCE_TYPE, true, true, placeholder=placeholder), '%'+clone_url+'%'
        )
        return [ScmPackage(row) for row in rows]

    def is_dir_in_commit(self, raw_commit_text):
        if not self.subdirectory or not raw_commit_text:
            return True

        for line in raw_commit_text.split('\n'):
            match = re.search(r'^(\+\+\+|---) [ab]/(\w*)/?.*$', line)
            if match and match.group(2).lower() == self.subdirectory.strip('./').lower():
                return True

        return False


def event_info_from_pr_update(data, base_url):
    """
    Message handler for updated pull-request opened in pagure.
    Topic: ``*.pagure.pull-request.comment.added``
    """
    if data['msg']['pullrequest']['status'] != 'Open':
        data.info('Pull-request not open, discarding.')
        return False

    if not data['msg']['pullrequest']['comments']:
        log.info('This is most odd, we\'re not seeing comments.')
        return False

    last_comment = data['msg']['pullrequest']['comments'][-1]
    if not last_comment or last_comment['notification'] is False:
        log.info('Comment was not a notification, discarding.')
        return False

    return munch.Munch({
        'object_id': data['msg']['pullrequest']['id'],
        'object_type': 'pull-request',
        'base_project_url_path': data['msg']['pullrequest']['project']['url_path'],
        'base_clone_url_path': data['msg']['pullrequest']['project']['fullname'],
        'base_clone_url': base_url + data['msg']['pullrequest']['project']['fullname'],
        'project_url_path': data['msg']['pullrequest']['repo_from']['url_path'],
        'clone_url_path': data['msg']['pullrequest']['repo_from']['fullname'],
        'clone_url': base_url + data['msg']['pullrequest']['repo_from']['fullname'],
        'branch_from': data['msg']['pullrequest']['branch_from'],
        'branch_to': data['msg']['pullrequest']['branch'],
        'start_commit': data['msg']['pullrequest']['commit_start'],
        'end_commit': data['msg']['pullrequest']['commit_stop'],
    })


def event_info_from_new_pr(data, base_url):
    """
    Message handler for new pull-request opened in pagure.
    Topic: ``*.pagure.pull-request.new``
    """
    return munch.Munch({
        'object_id': data['msg']['pullrequest']['id'],
        'object_type': 'pull-request',
        'base_project_url_path': data['msg']['pullrequest']['project']['url_path'],
        'base_clone_url_path': data['msg']['pullrequest']['project']['fullname'],
        'base_clone_url': base_url + data['msg']['pullrequest']['project']['fullname'],
        'project_url_path': data['msg']['pullrequest']['repo_from']['url_path'],
        'clone_url_path': data['msg']['pullrequest']['repo_from']['fullname'],
        'clone_url': base_url + data['msg']['pullrequest']['repo_from']['fullname'],
        'branch_from': data['msg']['pullrequest']['branch_from'],
        'branch_to': data['msg']['pullrequest']['branch'],
        'start_commit': data['msg']['pullrequest']['commit_start'],
        'end_commit': data['msg']['pullrequest']['commit_stop'],
    })


def event_info_from_push(data, base_url):
    """
    Message handler for push event in pagure.
    Topic: ``*.pagure.git.receive``
    """
    return munch.Munch({
        'object_id': data['msg']['end_commit'],
        'object_type': 'commit',
        'base_project_url_path': data['msg']['repo']['url_path'],
        'base_clone_url_path': data['msg']['repo']['fullname'],
        'base_clone_url': base_url + data['msg']['repo']['fullname'],
        'project_url_path': data['msg']['repo']['url_path'],
        'clone_url_path': data['msg']['repo']['fullname'],
        'clone_url': base_url + data['msg']['repo']['fullname'],
        'branch_from': data['msg']['branch'],
        'branch_to': data['msg']['branch'],
        'start_commit': data['msg']['start_commit'],
        'end_commit': data['msg']['end_commit'],
    })


def git_compare_urls(url1, url2):
    url1 = re.sub(r'(\.git)?/*$', '', url1)
    url2 = re.sub(r'(\.git)?/*$', '', url2)
    o1 = urlparse(url1)
    o2 = urlparse(url2)
    return (o1.netloc == o2.netloc and o1.path == o2.path)


def build_on_fedmsg_loop():
    log.debug("Setting up poller...")
    pp = pprint.PrettyPrinter(width=120)

    ctx = zmq.Context()
    s = ctx.socket(zmq.SUB)
    s.connect(ENDPOINT)

    for topic in TOPICS:
        s.setsockopt_string(zmq.SUBSCRIBE, topic)

    poller = zmq.Poller()
    poller.register(s, zmq.POLLIN)

    while True:
        log.debug('Polling...')
        evts = poller.poll(10000)
        if not evts:
            continue

        log.debug('Receiving...')
        _, msg_bytes = s.recv_multipart()
        msg = msg_bytes.decode('utf-8')

        log.debug('Parsing...')
        data = json.loads(msg)

        log.info('Got topic: {}'.format(data['topic']))
        base_url = TOPICS.get(data['topic'])
        if not base_url:
            log.error('Unknown topic {} received. Continuing.')
            continue

        if re.match(r'^.*.pull-request.new$', data['topic']):
            event_info = event_info_from_new_pr(data, base_url)
        elif re.match(r'^.*.pull-request.comment.added$', data['topic']):
            event_info = event_info_from_pr_update(data, base_url)
        else:
            event_info = event_info_from_push(data, base_url)

        log.info('event_info = {}'.format(pp.pformat(event_info)))

        if not event_info:
            log.info('Received event was discarded. Continuing.')
            continue

        candidates = ScmPackage.get_candidates_for_rebuild(event_info.base_clone_url)
        raw_commit_text = None
        if candidates:
            # if start_commit != end_commit
            # then more than one commit and no means to iterate over :(
            raw_commit_url = base_url + event_info.project_url_path + '/raw/' + event_info.end_commit
            r = requests.get(raw_commit_url)
            if r.status_code == requests.codes.ok:
                raw_commit_text = r.text
            else:
                log.error('Bad http status {0} from url {1}'.format(r.status_code, raw_commit_url))

        for pkg in candidates:
            log.info('Considering pkg id: {}, source_json: {}'.format(pkg.pkg_id, pkg.source_json_dict))

            if (git_compare_urls(pkg.clone_url, event_info.base_clone_url)
                    and (not pkg.committish or event_info.branch_to.endswith(pkg.committish))
                    and pkg.is_dir_in_commit(raw_commit_text)):

                log.info('\t -> rebuilding.')

                if event_info.object_type == 'pull-request':
                    dirname = pkg.copr.name + ':pr:' + str(event_info.object_id)
                    copr_dir = CoprDirsLogic.get_or_create(pkg.copr, dirname)
                    update_callback = 'pagure_flag_pull_request'
                    scm_object_url = os.path.join(base_url, event_info.base_project_url_path,
                                                  'pull-request', str(event_info.object_id))
                else:
                    copr_dir = pkg.copr.main_dir
                    update_callback = 'pagure_flag_commit'
                    scm_object_url = os.path.join(base_url, event_info.base_project_url_path,
                                                  'c', str(event_info.object_id))

                if not git_compare_urls(pkg.copr.scm_repo_url, event_info.base_clone_url):
                    update_callback = ''

                source_dict_update = {
                    'clone_url': event_info.clone_url,
                    'committish': event_info.end_commit,
                }

                build = pkg.build(
                    source_dict_update,
                    copr_dir,
                    update_callback,
                    event_info.object_type,
                    event_info.object_id,
                    scm_object_url
                )
                log.info('\t -> {}'.format(build.to_dict()))
                db.session.commit()
            else:
                log.info('\t -> skipping.')


if __name__ == '__main__':
    while True:
        try:
            build_on_fedmsg_loop()
        except KeyboardInterrupt:
            sys.exit(1)
        except:
            log.exception('Error in fedmsg loop. Restarting it.')
