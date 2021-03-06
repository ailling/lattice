from abc import ABCMeta, abstractmethod, abstractproperty
import logging

from utils import messages
from descriptors import TaskType, EndpointAddress

import zmq
import zhelpers
import helpers
from threading import Thread


class WorkerMeta(ABCMeta):
    def __new__(cls, name, bases, dct):
        if name not in ('Worker', 'MultiThreadedWorker', 'SingleThreadedWorker', 'ServiceWorker'):
            if 'task_type' in dct and not isinstance(dct['task_type'], TaskType):
                raise TypeError('task_type is required to be a TaskType enumerated value')

            if 'endpoint' in dct and not isinstance(dct['endpoint'], EndpointAddress):
                raise TypeError('endpoint is required to be an EndpointAddress object')

            if 'collector_endpoint' in dct and not isinstance(dct['collector_endpoint'], EndpointAddress):
                raise TypeError('collector_endpoint is requiredto be an EndpointAddress object')

            slots = []
            for key, value in dct.items():
                if isinstance(value, EndpointAddress):
                    value.name = '_' + key
                    slots.append(value.name)
                dct['__slots__'] = slots

        return super(WorkerMeta, cls).__new__(cls, name, bases, dct)




class Worker(object):
    """
    An abstract entity capable of doing work. This cannot be instantiated by client code.
    Instead, subclass and instantiate SingleThreadedWorker and MultiThreadedWorker

    Both SingleThreadedWorker and MultiThreadedWorker inherit from Worker.
    Note that MetaDataWorker does not inherit from Worker despite its name.
    """
    __metaclass__ = WorkerMeta

    @abstractproperty
    def task_type(self):
        return None

    @abstractproperty
    def endpoint(self):
        return ''

    @abstractproperty
    def collector_endpoint(self):
        return ''

    def __init__(self, *args, **kwargs):
        self.logger = logging.getLogger('zmqpipeline.worker')

        self.context = zmq.Context()

        self.logger.info('Worker connecting to collector endpoint: %s', self.collector_endpoint)
        self.sender = self.context.socket(zmq.PUSH)
        self.sender.connect(self.collector_endpoint)

        self.logger.info('Worker connecting to endpoint: %s', self.endpoint)
        self.worker = self.context.socket(zmq.REQ)
        zhelpers.set_id(self.worker)
        self.worker.connect(self.endpoint)

        self.worker_id = self.worker.getsockopt(zmq.IDENTITY)
        self.logger.info('Setting worker id: %s', self.worker_id)

        self.metadata = {}


    def send_init_msg(self):
        self.logger.info('Sending init message to %s' % self.endpoint)

        self.worker.send(b'')
        msg = self.worker.recv()
        data, _, msgtype = messages.get(msg)
        assert msgtype == messages.MESSAGE_TYPE_META_DATA

        self.metadata = data
        self.logger.info('Received init reply.')
        if self.metadata:
            self.logger.debug('Distributor has metadata: %s', self.metadata)

        initfn = getattr(self, 'init_worker', None)
        if initfn and callable(initfn):
            self.logger.info('Initialization method found. Invoking init_worker()')
            initfn()
        else:
            self.logger.info('No initialization method found (init_worker() not defined). Skipping initiliaization.')


    @abstractmethod
    def run(self):
        pass

    @abstractmethod
    def main_loop(self):
        pass

    def init_worker(self):
        """
        Optional implementation. When defined, this method is invoked after the worker
        sends and receives confirmation (from the distributor) of its readiness.

        Metadata will be stored on the worker at the time this method is executed.

        """
        pass


class MultiThreadedWorker(Worker):
    """
    A worker that processes data on multiple threads.

    Threads are instantiated and pooled at initialization time to minimize the cost of context switching.

    Morever, data is forwarded from the worker to each thread over the inproc protocol by default,
    which is significantly faster than tcp or ipc.

    """
    thread_endpoint = EndpointAddress('inproc://threadworker')

    @abstractproperty
    def n_threads(self):
        """
        The number of threads used.

        :return int: A positive integer
        """
        return 1

    def __init__(self, *args, **kwargs):
        super(MultiThreadedWorker, self).__init__(*args, **kwargs)
        self.thread_router = self.context.socket(zmq.REP)
        self.thread_router.bind(helpers.endpoint_binding(self.thread_endpoint))


    def worker_thread(self, worker_index, context):
        self.logger.info('Connecting thread %d to endpoint %s', worker_index, self.thread_endpoint)
        w = context.socket(zmq.REQ)
        zhelpers.set_id(w)
        w.connect(self.thread_endpoint)

        initfn = getattr(self, 'init_thread', None)
        if initfn and callable(initfn):
            self.logger.info('Thread initialization method found. Invoking init_thread()')
            initfn(worker_index = worker_index)
        else:
            self.logger.info('No thread initialization found (init_thread() undefined). Skipping thread initialization.')


        while True:
            w.send(messages.create_ready())

            msg = w.recv()
            data, tt, msgtype = messages.get(msg)

            assert tt == self.task_type

            if msgtype == messages.MESSAGE_TYPE_END:
                break

            data = data or {}
            self.logger.debug('Worker thread %d invoking handle_thread_execution with data: %s', worker_index, data)
            sdata = self.handle_thread_execution(data = data, index = worker_index)
            if sdata:
                data.update(sdata)

            self.logger.debug('Sending data to collector: %s', data)
            smsg = messages.create_data(self.task_type, data)
            self.sender.send(smsg)



    def init_threads(self):
        self.logger.info('Initializing %d threads', self.n_threads)
        for i in range(self.n_threads):
            Thread(target = self.worker_thread, args=[i, self.context]).start()

    def shutdown_threads(self):
        self.logger.info('Shutting down %d threads', self.n_threads)
        for _ in range(self.n_threads):
            self.thread_router.recv()
            self.thread_router.send(messages.create_end(task = self.task_type))


    def main_loop(self):
        self.logger.info('Multi threaded worker running at address %s, ID: %s', self.endpoint, self.worker_id)

        while True:
            self.worker.send(messages.create_ready(self.task_type))

            msg = self.worker.recv()
            data, tt, msgtype = messages.get(msg)
            assert tt == self.task_type

            if msgtype == messages.MESSAGE_TYPE_END:
                self.logger.info('Worker received END message. Shutting down threads.')
                self.shutdown_threads()
                self.logger.info('Threads are shutdown')
                break

            self.thread_router.recv()
            data = data or {}

            self.logger.debug('Invoking handle_execution with data: %s', data)
            sdata = self.handle_execution(data) or {}
            self.thread_router.send(messages.create_data(self.task_type, sdata))


    def run(self):
        self.init_threads()
        self.send_init_msg()
        self.main_loop()


    @abstractmethod
    def handle_execution(self, data, *args, **kwargs):
        """
        This method is invoked when the worker's main loop is executed. Client implementions
        of this method should, unlike the SingleThreadedWorker, not process data but instead
        forward the relevant data to the thread by returning a dictionary of information.

        :param dict data: A dictionary of data received by the worker
        :param args: Additional arguments
        :param kwargs: Additional keyword arguments

        :return dict: Data to be forwarded to the worker thread
        """
        return {}


    @abstractmethod
    def handle_thread_execution(self, data, index):
        """
        This method is invoked in the working thread. This is where data processing should be
        handled.

        :param dict data: A dictionary of data provided by the worker
        :param int index: The index number of the thread that's been invoked
        :return dict: A dictionary of information to be forwarded to the collector
        """
        return {}



class SingleThreadedWorker(Worker):
    """
    A worker that processes data on a single thread
    """
    def main_loop(self):
        self.logger.info('Single threaded worker running at address %s, ID: %s', self.endpoint, self.worker_id)

        while True:
            self.worker.send(messages.create_ready(self.task_type))
            msg = self.worker.recv()
            data, tt, msgtype = messages.get(msg)
            assert tt == self.task_type

            if msgtype == messages.MESSAGE_TYPE_END:
                self.logger.info('Worker received END message')
                break

            data = data or {}
            self.logger.debug('Worker invoking handle_execution on task type %s with data: %s', self.task_type, data)
            sdata = self.handle_execution(data) or {}

            self.logger.debug('Worker sending results from task type: %s - data: %s', self.task_type, sdata)
            smsg = messages.create_data(self.task_type, sdata)
            self.sender.send(smsg)


    def run(self):
        self.send_init_msg()
        self.main_loop()


    @abstractmethod
    def handle_execution(self, data, *args, **kwargs):
        """
        Invoked in the worker's main loop whenever a task is received from the distributor.
        This is where client implemntations should process data and forward results to the collector.

        :param dict data: Data provided as a dictionary from the distributor
        :param args: A list of additional positional arguments
        :param kwargs: A list of additional keyword arguments
        :return: A dictionary of data to be passed to the collector, or None, in which case no data will be forwarded to the collector
        """
        return {}



class MetaDataWorker(object):
    """
    Transmits meta information to the distributor for dynamic configuration at runtime.
    When using a meta data worker, the Distributor should be instantiated with receive_metadata boolean
    turned on.
    """
    __metaclass__ = ABCMeta

    @abstractproperty
    def endpoint(self):
        return ''


    def __init__(self):
        self.context = zmq.Context()
        self.worker = self.context.socket(zmq.REQ)
        zhelpers.set_id(self.worker)
        self.worker.connect(self.endpoint)
        self.logger = logging.getLogger('zmqpipeline.metadataworker')


    def send_metadata(self):
        self.logger.info('Collecting meta data')

        metadata = self.get_metadata()
        if not isinstance(metadata, dict):
            raise TypeError('MetaDataWorker.get_metadata must return a dictionary')

        self.logger.info('Sending metadata: %s', metadata)
        self.worker.send(messages.create_metadata(metadata))

        self.worker.recv()
        self.logger.info('Meta data sent. Success reply received')


    def run(self):
        """
        Runs the meta worker, sending meta data to the distributor.
        :return:
        """
        self.send_metadata()


    @abstractmethod
    def get_metadata(self):
        """
        Retrieves meta data to be sent to tasks and workers.
        :return: A dictionary of meta data
        """
        return {}



class ServiceWorker(object):
    """
    A worker that plugs into a service. Regular workers plug into a distributor / collector pair
    """
    __metaclass__ = WorkerMeta

    @abstractproperty
    def task_type(self):
        """
        A registered task type, or a blank string.

        :return TaskType: A properly registered task type or an empty string
        """
        return ''


    @abstractproperty
    def endpoint(self):
        """
        The address of the broker's backend

        :return EndpointAddress: A valid EndpointAddress instance
        """
        return ''

    def __init__(self, id_prefix='worker'):
        self.logger = logging.getLogger('zmqpipeline.serviceworker')

        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.REQ)
        self.worker_id = zhelpers.set_id(self.socket, prefix = id_prefix)

        self.logger.info('Worker %s connecting to endpoint %s', self.worker_id, self.endpoint)
        self.socket.connect(self.endpoint)

        self.init_sent = False


    @abstractmethod
    def handle_message(self, data, task_type, msgtype):
        """
        Overridden by subclassed ServiceWorkers.

        :param data: Data the client has submitted for processing, typically in a dictionary
        :param TaskType task_type: A registered TaskType enumerated value
        :param str msgtype: A message type enumerated value
        :return: Data to be sent back to the requesting client, typically a dictionary
        """
        pass


    def run(self):
        """
        Run the service worker. This call blocks until an END message is received from the broker

        :return: None
        """

        while True:
            if not self.init_sent:
                self.logger.debug('Worker %s sending initialization signal', self.worker_id)
                self.socket.send(messages.create_ready(task = self.task_type))
                self.init_sent = True
                continue

            addr, empty, msg = self.socket.recv_multipart()

            data, tt, msgtype = messages.get(msg)
            self.logger.debug('Service worker %s received task type: %s, message type: %s, data: %s', self.worker_id, tt, msgtype, str(data))

            if msgtype == messages.MESSAGE_TYPE_END:
                self.logger.info('Worker %s received END message', self.worker_id)
                break

            # invoke client implementation
            retdata = self.handle_message(data, tt, msgtype)
            self.logger.debug('Service worker %s responding to %s with data: %s', self.worker_id, addr, str(retdata))

            self.socket.send_multipart([
                addr, b'', messages.create_data(tt, retdata)
            ])

