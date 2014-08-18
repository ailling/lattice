import settings
import zmqpipeline

class CalculationTask(zmqpipeline.Task):
    task_type = zmqpipeline.TaskType(settings.TASK_TYPE_CALC)
    endpoint = zmqpipeline.EndpointAddress(settings.WORKER_ENDPOINT)
    dependencies = []
    n_count = 0

    def handle(self, data, address, msgtype, ack_data={}):
        """
        Simulates some work to be done
        :param data: Data received from distributor
        :param address: The address of the client where task output will be sent
        :param msgtype: the type of message received. Typically zmqpipeline.utils.messages.MESSAGE_TYPE_DATA
        :return:
        """
        self.n_count += 1
        if self.n_count >= 500:
            # mark as complete after 1000 executions. Should take a total of 10 seconds to run sequentially
            self.is_complete = True

        print 'handling - n_count: %d' % self.n_count

        # return the work to be done on the worker
        return {
            'workload': .01
        }

