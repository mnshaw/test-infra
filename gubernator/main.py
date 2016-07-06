#!/usr/bin/env python

# Copyright 2016 The Kubernetes Authors All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
import json
import logging
import re
import os
import urllib
import zlib

import webapp2
import jinja2
import yaml

from google.appengine.api import memcache
import google.appengine.ext.ndb as ndb

import defusedxml.ElementTree as ET
import cloudstorage as gcs

import filters
import log_parser

BUCKET_WHITELIST = {
    re.match(r'gs://([^/]+)', path).group(1)
    for path in yaml.load(open("buckets.yaml"))
}

DEFAULT_JOBS = {
    'kubernetes-jenkins/logs/': {
        'kubelet-gce-e2e-ci',
        'kubernetes-build',
        'kubernetes-e2e-gce',
        'kubernetes-e2e-gce-scalability',
        'kubernetes-e2e-gce-slow',
        'kubernetes-e2e-gke',
        'kubernetes-e2e-gke-slow',
        'kubernetes-kubemark-5-gce',
        'kubernetes-kubemark-500-gce',
        'kubernetes-test-go',
    }
}

PR_PREFIX = 'kubernetes-jenkins/pr-logs/pull'

GCS_API_URL = 'https://storage.googleapis.com'
STORAGE_API_URL = 'https://www.googleapis.com/storage/v1/b'

JINJA_ENVIRONMENT = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__) + '/templates'),
    extensions=['jinja2.ext.autoescape'],
    trim_blocks=True,
    autoescape=True)
JINJA_ENVIRONMENT.line_statement_prefix = '%'
filters.register(JINJA_ENVIRONMENT.filters)


def pad_numbers(s):
    """Modify a string to make its numbers suitable for natural sorting."""
    return re.sub(r'\d+', lambda m: m.group(0).rjust(16, '0'), s)


def memcache_memoize(prefix, expires=60 * 60, neg_expires=60):
    """Decorate a function to memoize its results using memcache.

    The function must take a single string as input, and return a pickleable
    type.

    Args:
        prefix: A prefix for memcache keys to use for memoization.
        expires: How long to memoized values, in seconds.
        neg_expires: How long to memoize falsey values, in seconds
    Returns:
        A decorator closure to wrap the function.
    """
    # setting the namespace based on the current version prevents different
    # versions from sharing cache values -- meaning there's no need to worry
    # about incompatible old key/value pairs
    namespace = os.environ['CURRENT_VERSION_ID']
    def wrapper(func):
        @functools.wraps(func)
        def wrapped(arg):
            key = prefix + arg
            data = memcache.get(key, namespace=namespace)
            if data is not None:
                return data
            else:
                data = func(arg)
                if data:
                    memcache.add(key, data, expires, namespace=namespace)
                else:
                    memcache.add(key, data, neg_expires, namespace=namespace)
                return data
        return wrapped
    return wrapper


@ndb.tasklet
def gcs_get_async(url):
    context = ndb.get_context()
    headers = {'accept-encoding': 'gzip, *', 'x-goog-api-version': '2'}
    for retry in xrange(6):
        result = yield context.urlfetch(url, headers=headers)
        status = result.status_code
        if status == 429 or 500 <= status < 600:
            yield ndb.sleep(2 ** retry)
            continue
        if status in (200, 206):
            content = result.content
            if result.headers.get('content-encoding') == 'gzip':
                content = zlib.decompress(result.content, 15 | 16)
            raise ndb.Return(content)
        logging.error("unable to fetch '%s': status code %d" % (url, status))
        raise ndb.Return(None)


def gcs_read_async(path):
    """
    Asynchronously reads a file from GCS.

    NOTE: for large files (>10MB), this may return a truncated response due to
    urlfetch API limits. We don't want to read large files anyways, so this is
    fine.

    Args:
        path: the location of the object to read
    Returns:
        a Future that resolves to the file's data, or None if an error occurred.
    """
    url = GCS_API_URL + path
    return gcs_get_async(url)


@ndb.tasklet
def gcs_listdirs_async(path):
    """
    Asynchronously list directories present on GCS.

    NOTE: This returns at most 1000 results. The API supports pagination, but
    it's not yet been implemented.

    Args:
        path: the GCS bucket directory to list subdirectories of
    Returns:
        a Future that resolves to a list of directories, or None if an error
        occurred.
    """
    if path[-1] != '/':
        path += '/'
    assert path[0] != '/'
    bucket, prefix = path.split('/', 1)
    url = '%s/%s/o?delimiter=/&prefix=%s' % (STORAGE_API_URL, bucket, prefix)
    res = yield gcs_get_async(url)
    if res is None:
        raise ndb.Return(None)
    prefixes = json.loads(res).get('prefixes', [])
    raise ndb.Return(['%s/%s' % (bucket, prefix) for prefix in prefixes])


@memcache_memoize('gs-ls://', expires=60)
def gcs_ls(path):
    """Enumerate files in a GCS directory. Returns a list of FileStats."""
    if path[-1] != '/':
      path += '/'
    return list(gcs.listbucket(path, delimiter='/'))

def get_pod_name(text):
    """Find the pod name from the failure and return the pod name."""
    p = re.search(r'(.*) pod (.*?) .*', text)
    print ""
    if p:
        remove = re.compile(r'(\'|\"|\\)')
        return remove.sub('', p.group(2))                     
    else: 
        return ""

def parse_junit(xml):
    """Generate failed tests as a series of (name, duration, text) tuples."""
    tree = ET.fromstring(xml)
    if tree.tag == 'testsuite':
        for child in tree:
            name = child.attrib['name']
            time = float(child.attrib['time'])
            for param in child.findall('failure'):
                pod_name = get_pod_name(param.text)            
                yield name, time, param.text, pod_name
    elif tree.tag == 'testsuites':
        for testsuite in tree:
            suite_name = testsuite.attrib['name']
            for child in testsuite.findall('testcase'):
                name = '%s %s' % (suite_name, child.attrib['name'])
                time = float(child.attrib['time'])
                for param in child.findall('failure'):
                    pod_name = get_pod_name(param.text)
                    yield name, time, param.text, pod_name
    else:
        logging.error('unable to find failures, unexpected tag %s', tree.tag)


@memcache_memoize('build-details://', expires=60 * 60 * 4)
def build_details(build_dir):
    """
    Collect information from a build directory.

    Args:
        build_dir: GCS path containing a build's results.
    Returns:
        started: value from started.json {'version': ..., 'timestamp': ...}
        finished: value from finished.json {'timestamp': ..., 'result': ...}
        failures: list of (name, duration, text) tuples
        build_log: a hilighted portion of errors in the build log. May be None.
    """
    started_fut = gcs_read_async(build_dir + '/started.json')
    finished = gcs_read_async(build_dir + '/finished.json').get_result()
    started = started_fut.get_result()
    if finished and not started:
        started = 'null'
    if started and not finished:
        finished = 'null'
    elif not (started and finished):
        return
    started = json.loads(started)
    finished = json.loads(finished)

    failures = []
    junit_futures = {}
    junit_paths = [f.filename for f in gcs_ls('%s/artifacts' % build_dir)
                   if re.match(r'junit_.*\.xml', os.path.basename(f.filename))]
    
    fps = []
    last_len = 0
    total_len = 0
    for f in junit_paths:
        junit_futures[gcs_read_async(f)] = f

    for future in junit_futures:
        junit = future.get_result()
        if junit is None:
            continue
        failures.extend(parse_junit(junit))
        total_len = len(fps)
        last_len = len(failures)-total_len
        for i in xrange(last_len):
            fps.append(junit_futures[future])

    build_log = None
    if finished and finished.get('result') != 'SUCCESS' and len(failures) == 0:
        build_log = gcs_read_async(build_dir + '/build-log.txt').get_result()
        if build_log:
            build_log = log_parser.digest(build_log.decode('utf8', 'replace'))
            logging.info('fallback log parser emitted %d lines',
                         build_log.count('\n'))
    return started, finished, failures, build_log, fps

def parse_kubelet(pod, junit, build_dir):
    # print pod
    # print junit
    # print build_dir
    junit_file = "junit_" + junit + ".xml"


class RenderingHandler(webapp2.RequestHandler):
    """Base class for Handlers that render Jinja templates."""
    def render(self, template, context):
        """Render a context dictionary using a given template."""
        template = JINJA_ENVIRONMENT.get_template(template)
        self.response.write(template.render(context))

    def check_bucket(self, prefix):
        if prefix in BUCKET_WHITELIST:
            return
        if prefix[:prefix.find('/')] not in BUCKET_WHITELIST:
            self.abort(404)


class IndexHandler(RenderingHandler):
    """Render the index."""
    def get(self):
        self.render("index.html", {'jobs': DEFAULT_JOBS})


class BuildHandler(RenderingHandler):
    """Show information about a Build and its failing tests."""
    def get(self, prefix, job, build):
        self.check_bucket(prefix)
        job_dir = '/%s/%s/' % (prefix, job)
        build_dir = job_dir + build
        details = build_details(build_dir)
        if not details:
            logging.warning('unable to load %s', build_dir)
            self.render('build_404.html', {"build_dir": build_dir})
            self.response.set_status(404)
            return
        started, finished, failures, build_log, fps = details
        
        # map failure to the junit file it was in
        failures_files = {}
        for i in xrange(len(failures)):
            failures_files[failures[i]] = fps[i] 
        
        junit_file = {}
        for fp in fps:
            num = re.search(r'.*(\d\d)\.xml', fp)
            junit_file[fp] = num.group(1)
        # pass in this to the render thing

        if started:
            commit = started['version'].split('+')[-1]
        else:
            commit = None
        pr = None
        logging.info("PREFIX: %s / %s", prefix, PR_PREFIX)
        if prefix.startswith(PR_PREFIX):
            pr = os.path.basename(prefix)
        self.render('build.html', dict(
            job_dir=job_dir, build_dir=build_dir, job=job, build=build,
            commit=commit, started=started, finished=finished,
            failures=failures, build_log=build_log, pr=pr, fps=failures_files,
            junits=junit_file))


class BuildListHandler(RenderingHandler):
    """Show a list of Builds for a Job."""
    def get(self, prefix, job):
        self.check_bucket(prefix)
        job_dir = '/%s/%s/' % (prefix, job)
        fstats = gcs_ls(job_dir)
        fstats.sort(key=lambda f: pad_numbers(f.filename), reverse=True)
        self.render('build_list.html',
                    dict(job=job, job_dir=job_dir, fstats=fstats))

class NodeLogHandler(RenderingHandler):
    def get(self, prefix, job, build):
        self.check_bucket(prefix)
        job_dir = '/%s/%s/' % (prefix, job)
        build_dir = job_dir + build
        pod_name = self.request.get("pod")
        junit = self.request.get("junit")
        parse_kubelet(pod_name, junit, build_dir)
        self.render('node_404.html', {"build_dir": build_dir, 
            "pod_name":pod_name, "junit":junit})
        self.response.set_status(404)
        return

class JobListHandler(RenderingHandler):
    """Show a list of Jobs in a directory."""
    def get(self, prefix):
        self.check_bucket(prefix)
        jobs_dir = '/%s' % prefix
        fstats = gcs_ls(jobs_dir)
        fstats.sort()
        self.render('job_list.html', dict(jobs_dir=jobs_dir, fstats=fstats))


@memcache_memoize('pr-details://', expires=60 * 3)
def pr_details(pr):
    jobs_dirs_fut = gcs_listdirs_async('%s/%s' % (PR_PREFIX, pr))

    def base(path):
        return os.path.basename(os.path.dirname(path))

    jobs_futures = [(job, gcs_listdirs_async(job)) for job in jobs_dirs_fut.get_result()]
    futures = []

    for job, builds_fut in jobs_futures:
        for build in builds_fut.get_result():
            fut = gcs_read_async('/%sfinished.json' % build)
            futures.append([base(job), base(build), fut])

    jobs = {}

    futures.sort(key=lambda (job, build, _): (job, pad_numbers(build)), reverse=True)

    for job, build, fut in futures:
        res = fut.get_result()
        if res is None:
            status = '???'
        else:
            status = json.loads(res).get('result', '???')
        jobs.setdefault(job, []).append((build, status))

    return jobs


class PRHandler(RenderingHandler):
    """Show a list of test runs for a PR."""
    def get(self, pr):
        details = pr_details(pr)
        max_builds = max(len(builds) for builds in details.values() + [[]])
        self.render('pr.html', dict(pr=pr, prefix=PR_PREFIX,
            details=details, max_builds=max_builds))


app = webapp2.WSGIApplication([
    (r'/', IndexHandler),
    (r'/jobs/(.*)$', JobListHandler),
    (r'/builds/(.*)/([^/]+)/?', BuildListHandler),
    (r'/build/(.*)/([^/]+)/(\d+)/?', BuildHandler),
    (r'/build/(.*)/([^/]+)/(\d+)/nodelog*', NodeLogHandler),
    (r'/pr/(\d+)', PRHandler),
], debug=True)
