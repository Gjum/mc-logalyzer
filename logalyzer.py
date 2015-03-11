import glob
import gzip
import datetime
import logging
import time
import os
import re
import yaml

logging.basicConfig(format='[%(asctime)s] [%(levelname)s] %(funcName)s@%(lineno)s: %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('logalyzer')
logger.setLevel(logging.INFO)

log_actions = []
def log_action(regex_str):
    def inner(fun):
        regex_comp = re.compile(regex_str)
        log_actions.append((regex_comp, fun))
        return fun
    return inner

def date_str_to_epoch(day_str, time_str='00:00:00'):
    epoch = int(time.mktime(datetime.datetime.strptime(day_str + ' ' + time_str, '%Y-%m-%d %H:%M:%S').timetuple()))
    return epoch


class LogFile:
    RE_TIME = re.compile('^\[([\d:]{8})\] ')
    RE_START = re.compile('^\[([\d:]{8})\] \[Server thread/INFO\]: Starting minecraft server version ')

    def __init__(self, parent, log_name='latest'):
        self.parent = parent
        self.log_name = log_name
        self.log_path = '%s/%s.log' % (parent.logs_dir, log_name)
        self.uuids = {}  # name -> last associated UUID
        self.been_read = False

        self.yaml_attributes = 'started', 'stopped', 'first_event', 'last_event', 'online', 'times'
        self.started = None
        self.stopped = None
        self.first_event = None
        self.last_event = None
        self.online = {}
        self.times = []

        logger.info('LogFile %s', self.log_name)

    def read_log(self, prev_logs=()):
        logger.info('prev_logs: %s', prev_logs)
        if self.been_read:
            logger.warn('Already read %s', self.log_name)
            return
        try:
            yaml_file = open(self.log_path + '.yaml', 'r')
        except FileNotFoundError:
            # no converted file exists, create it
            if not self.peek_start():
                if len(prev_logs) > 0:
                    prev_log = prev_logs[-1]
                    prev_log.read_log(prev_logs[:-1])
                    self.online = prev_log.online
                else:
                    raise ValueError('First log and no server start')
            self.convert_log()
            self.write_yaml()
        else:  # converted file exists, read it
            with yaml_file:
                data = yaml.load(yaml_file)
                for attr in self.yaml_attributes:
                    setattr(self, attr, data[attr])
        self.been_read = True
        for attr in self.yaml_attributes:
            logger.debug('%s.%s = %s' % (self.log_name, attr, getattr(self, attr)))
        logger.debug('Done reading %s ------------------------------', self.log_name)

    def convert_log(self):
        if self.log_name == 'latest':
            log_file = open(self.log_path, 'rb')
        else:
            log_file = gzip.open(self.log_path + '.gz', 'rb')
        with log_file:
            day_str = self.log_name.rsplit('-', 1)[0]
            line_no = 0  # TODO TMP logging only
            for line in log_file:
                line_no += 1
                line = line.decode()
                time_match = self.RE_TIME.match(line)
                if time_match:  # only look at lines with a timestamp
                    time_str = line[1:9]
                    seconds = date_str_to_epoch(day_str, time_str)
                    if self.first_event is None:
                        self.first_event = seconds
                    if self.last_event is None or self.last_event < seconds:
                        self.last_event = seconds
                    line_after_time = line[11:]  # strip off the `[12:34:56] `
                    for regex, action in log_actions:
                        match = regex.match(line_after_time)
                        if match:
                            args = match.groups()
                            logger.debug('%s (%2i %i) %s: %s' % (self.log_name, line_no, seconds, action.__name__, args))
                            action(self, seconds, *args)
        if self.stopped:
            for name in list(self.online.keys())[:]:
                self.found_leave(self.last_event, name, 'Server Stop')
                logger.info('Stop leaving %s at %s' % (name, self.log_name))

    @log_action('^\[User Authenticator #(\d+)/INFO\]: UUID of player ([^ ]+) is ([-\da-f]{36})$')
    def found_uuid(self, seconds, auth_nr, name, uuid):
        self.uuids[name] = uuid

    @log_action('^\[Server thread/INFO\]: ([^ \[]+)\[([/\d\.:]+)\] logged in with entity id (\d+) at \(([-\d\.]+), ([-\d\.]+), ([-\d\.]+)\)$')
    def found_join(self, seconds, name, ip, e_id, x, y, z):
        if name in self.online:
            logger.warn('double join %s, at %s %i', name, self.log_name, seconds)
        else:
            self.online[name] = [self.uuids[name], seconds]

    @log_action('^\[Server thread/INFO\]: ([^ ]+) lost connection: (.*)$')
    def found_leave(self, seconds, name, reason):
        if "text='You logged in from another location'" in reason:
            logger.warn('double leave %s ignored, at %s %i', name, self.log_name, seconds)
            return
        if name not in self.online:
            raise ValueError('Player %s left without joining at %s %i' % (name, self.log_name, seconds))
        uuid, from_sec = self.online[name]
        del self.online[name]
        self.times.append([uuid, from_sec, seconds, name])

    @log_action('\[Server thread/INFO\]: Stopping server$')
    def found_stop(self, seconds):
        if self.stopped:
            logger.error('Stopped two times at %s %i', self.log_name, seconds)
        self.stopped = True

    def write_yaml(self):
        if self.log_name == 'latest':
            logger.warn('not writing latest, aborting')
            return
        with open(self.log_path + '.yaml', 'w') as yaml_file:
            logger.warn('writing %s', self.log_path + '.yaml')
            data = {}
            for attr in self.yaml_attributes:
                data[attr] = getattr(self, attr)
            yaml.dump(data, stream=yaml_file)

    def peek_start(self):
        if self.started is not None:
            # already peeked
            logger.warn('already peeked')
            return self.started
        if self.log_name == 'latest':
            log_file = open(self.log_path, 'r')
        else:
            log_file = gzip.open(self.log_path + '.gz', 'rb')
        with log_file:
            for line in log_file:
                line = line.decode()
                self.started = bool(self.RE_START.match(line))
                logger.debug('in log: %s', self.started)
                return self.started
            self.started = False  # empty log
            logger.error('empty log')
            return self.started


class AllLogs:
    def __init__(self, logs_dir):
        self.logs_dir = logs_dir
        self.logs = []
        self.sorted_split_log_names = self.collect_sorted_split_log_names()

    def collect_sorted_split_log_names(self):
        unsorted_paths = glob.iglob(self.logs_dir + '/*.log.gz')
        unsorted_log_names = map(lambda p: os.path.split(p)[1][:-7], unsorted_paths)
        sorted_split_log_names = sorted(map(lambda s: [int(i) for i in s.split('-')], unsorted_log_names))
        return sorted_split_log_names

    def read_interval(self, from_day=None, to_day=None):
        """
        from_day, to_day are in format yyyy-mm-dd
        """
        logger.debug('from_day=%s to_day=%s', from_day, to_day)
        logs_before = self.get_split_log_names_between(self.sorted_split_log_names, None, to_day)
        for log_split in self.get_split_log_names_between(logs_before, from_day, to_day):
            log_name = self.join_split_name(log_split)
            log = LogFile(self, log_name)
            log.read_log(self.logs)
            self.logs.append(log)
            # TODO read_interval

    @staticmethod
    def get_split_log_names_between(logs_in_range, from_log=None, to_log=None):
        """
        from_log, to_log are in format yyyy-mm-dd or yyyy-mm-dd-n,
        from_log may be None to accept all logs before to_log,
        to_log may be None to accept all logs after from_log
        """
        logger.debug('from_log=%s to_log=%s, sorted_split_log_names=%s', from_log, to_log, logs_in_range)
        if from_log is not None:
            from_split = [int(i) for i in from_log.split('-')]
            logs_in_range = filter(lambda log: from_split <= log, logs_in_range)
        if to_log is not None:
            to_split = [int(i) for i in to_log.split('-')]
            logs_in_range = filter(lambda log: log < to_split, logs_in_range)
        logs_in_range = list(logs_in_range)  # TODO logging only =(
        logger.debug('returning %s', logs_in_range)
        return logs_in_range

    @staticmethod
    def join_split_name(log_split):
        return '%i-%02i-%02i-%i' % tuple(log_split)


if __name__ == '__main__':
    f = AllLogs("test_logs/")
    # f.read_interval("2015-01-01")
    f.read_interval()