"""
KBase Job Manager

The main class here defines a manager for running jobs (as Job objects).
This class knows how to fetch job status, kill jobs, etc.
It also communicates with the front end over the KBaseJobs channel.

It is intended for use as a singleton - use the get_manager() function
to fetch it.
"""
__author__ = "Bill Riehl <wjriehl@lbl.gov>"
__version__ = "0.0.1"

import biokbase.narrative.clients as clients
from .job import Job
from ipykernel.comm import Comm
import threading
import json
import logging
from biokbase.narrative.common import kblogging
from biokbase.narrative.common.log_common import EVENT_MSG_SEP
from IPython.display import HTML
from jinja2 import Template
import dateutil.parser
import datetime

class JobManager(object):
    """
    The KBase Job Manager clsas. This handles all jobs and makes their status available.
    On status lookups, it feeds the results to the KBaseJobs channel that the front end
    listens to.
    """
    __instance = None

    running_jobs = dict()
    _lookup_timer = None
    _comm = None
    _log = kblogging.get_logger(__name__)
    _log.setLevel(logging.INFO)

    def __new__(cls):
        if JobManager.__instance is None:
            JobManager.__instance = object.__new__(cls)
        return JobManager.__instance


    def initialize_jobs(self, job_tuples):
        """
        Initializes this JobManager with a list of running jobs. These are all expected to
        be NJS-wrapped Jobs - i.e. only those jobs made with the SDK (not earlier service-based
        jobs, or jobs only found in the UJS).

        This is expected to be run with a list of Jobs that have already been initialized.
        For example, if the kernel is restarted and the synching needs to be redone from the
        front end, this is the function that gets invoked.

        Parameters:
        -----------
        job_tuples: A list of 4-tuples representing Jobs. The format is:
            (job_id (string),
             set of job inputs (as a JSON string),
             version tag (string),
             cell id that started the job (string or None))
        """
        for job_tuple in job_tuples:
            if job_tuple[0] not in self.running_jobs:
                try:
                    self.running_jobs[job_tuple[0]] = self._get_existing_job(job_tuple)

                except Exception, e:
                    self._log.setLevel(logging.ERROR)
                    kblogging.log_event(self._log, "init_error", {'err': str(e)})
                    self._send_comm_message('job_init_err', str(e))
        # only keep one loop at a time in cause this gets called again!
        if self._lookup_timer is not None:
            self._lookup_timer.cancel()
        self.lookup_job_status_loop()

    def list_jobs(self):
        """
        List all job ids, their info, and status in a quick HTML format.
        """
        try:
            status_set = list()
            for job_id in self.running_jobs:
                job_state = self.running_jobs[job_id].state()
                status_set.append(job_state)
            if not len(status_set):
                return "No running jobs!"
            status_set = sorted(status_set, key=lambda s: dateutil.parser.parse(s['submit_time']))
            for i in range(len(status_set)):
                status_set[i]['submit_time'] = datetime.datetime.strftime(dateutil.parser.parse(status_set[i]['submit_time']), "%Y-%m-%d %H:%M:%S")
                meth_id = status_set[i]['original_app']['steps'][0]['method_spec_id']
                stats = status_set[i]['step_stats'][meth_id]
                exec_start = stats.get('exec_start_time', None)
                if 'complete_time' in status_set[i]:
                    status_set[i]['complete_time'] = datetime.datetime.strftime(dateutil.parser.parse(status_set[i]['complete_time']), "%Y-%m-%d %H:%M:%S")
                    finished = stats.get('finish_time', None)
                    if finished and exec_start:
                        delta = datetime.datetime.fromtimestamp(finished/1000.0) - datetime.datetime.fromtimestamp(exec_start/1000.0)
                        delta = delta - datetime.timedelta(microseconds=delta.microseconds)
                        status_set[i]['run_time'] = str(delta)
                elif exec_start:
                    delta = datetime.datetime.utcnow() - datetime.datetime.utcfromtimestamp(exec_start/1000.0)
                    delta = delta - datetime.timedelta(microseconds=delta.microseconds)
                    status_set[i]['run_time'] = str(delta)
                else:
                    status_set[i]['run_time'] = 'Not started'

            tmpl = """
            <table class="table table-bordered table-striped table-condensed">
                <tr>
                    <th>Id</th>
                    <th>Name</th>
                    <th>Submitted</th>
                    <th>Status</th>
                    <th>Run Time</th>
                    <th>Complete Time</th>
                </tr>
                {% for j in jobs %}
                <tr>
                    <td>{{ j.job_id|e }}</td>
                    <td>{{ j.original_app.steps[0].method_spec_id|e }}</td>
                    <td>{{ j.submit_time|e }}</td>
                    <td>{{ j.job_state|e }}</td>
                    <td>{{ j.run_time|e }}</td>
                    <td>{% if j.complete_time %}{{ j.complete_time|e }}{% else %}Incomplete{% endif %}</td>
                </tr>
                {% endfor %}
            </table>
            """
            return HTML(Template(tmpl).render(jobs=status_set))

        except Exception, e:
            self._log.setLevel(logging.ERROR)
            kblogging.log_event(self._log, "list_jobs.error", {'err': str(e)})
            raise

    def get_jobs_list(self):
        """
        A convenience method for fetching an unordered list of all running Jobs.
        """
        return self.running_jobs.values()

    def _get_existing_job(self, job_tuple):
        """
        creates a Job object from a job_id that already exists.
        If no job exists, raises an Exception.

        Parameters:
        -----------
        job_tuple : The expected 4-tuple representing a Job. The format is:
            (job_id, set of job inputs (as JSON), version tag, cell id that started the job)
        """

        # remove the prefix (if present) and take the last element in the split
        job_id = job_tuple[0].split(':')[-1]
        try:
            job_info = clients.get('job_service').get_job_params(job_id)[0]
            return Job.from_state(job_id, job_info, tag=job_tuple[2], cell_id=job_tuple[3])
        except Exception, e:
            self._log.setLevel(logging.ERROR)
            kblogging.log_event(self._log, "get_existing_job.error", {'job_id': job_id, 'err': str(e)})
            raise

    def lookup_job_status(self):
        """
        Starts the job status lookup. This then optionally spawns a timer that
        looks up the status again after a few seconds.

        Once job info is acquired, it gets pushed to the front end over the
        'KBaseJobs' channel.
        """
        status_set = dict()
        for job_id in self.running_jobs:
            try:
                status_set[job_id] = {'state': self.running_jobs[job_id].state(),
                                      'spec': self.running_jobs[job_id].app_spec()}
            except Exception, e:
                self._log.setLevel(logging.ERROR)
                kblogging.log_event(self._log, "lookup_job_status.error", {'err': str(e)})
                self._send_comm_message('job_err', {
                    'job_id': job_id,
                    'message': str(e)
                })
        self._send_comm_message('job_status', status_set)

    def lookup_job_status_loop(self):
        """
        Initialize a loop that will look up job info. This uses a Timer thread on a 10
        second loop to update things.
        """
        self.lookup_job_status()
        self._lookup_timer = threading.Timer(10, self.lookup_job_status_loop)
        self._lookup_timer.start()

    def cancel_job_lookup_loop(self):
        """
        Cancels a running timer if one's still alive.
        """
        if self._lookup_timer:
            self._lookup_timer.cancel()

    def register_new_job(self, job):
        """
        Registers a new Job with the manager - should only be invoked when a new Job gets
        started. This stores the Job locally and pushes it over the comm channel to the
        Narrative where it gets serialized.

        Parameters:
        -----------
        job : biokbase.narrative.jobs.job.Job object
            The new Job that was started.
        """
        self.running_jobs[job.job_id] = job
        # push it forward! create a new_job message.
        self._send_comm_message('new_job', {
            'id': job.job_id,
            'app_id': job.app_id,
            'inputs': job.inputs,
            'version': job.app_version,
            'tag': job.tag,
            'cell_id': job.cell_id
        })

    def get_job(self, job_id):
        """
        Returns a Job with the given job_id.
        Raises a ValueError if not found.
        """
        if job_id in self.running_jobs:
            return self.running_jobs[job_id]
        else:
            raise ValueError('No job present with id {}'.format(job_id))

    def _handle_comm_message(self, msg):
        if 'request_type' in msg['content']['data']:
            r_type = msg['content']['data']['request_type']
            if r_type == 'refresh_all':
                self.lookup_job_status()

    def _send_comm_message(self, msg_type, content):
        """
        Sends a ipykernel.Comm message to the KBaseJobs channel with the given msg_type
        and content. These just get encoded into the message itself.
        """
        msg = {
            'msg_type': msg_type,
            'content': content
        }
        if self._comm is None:
            self._comm = Comm(target_name='KBaseJobs', data={})
            self._comm.on_msg(self._handle_comm_message)
        self._comm.send(msg)