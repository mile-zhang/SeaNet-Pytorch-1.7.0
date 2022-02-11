import sys
import threading
import queue
import random
import collections

import torch
import torch.multiprocessing as multiprocessing

# from torch._C import _set_worker_signal_handlers, _update_worker_pids, \
#     _remove_worker_pids, _error_if_any_worker_fails
from torch._C import _set_worker_signal_handlers
from torch.utils.data import _utils #new 
from torch.utils.data.dataloader import DataLoader
from torch.utils.data.dataloader import _SingleProcessDataLoaderIter  # _DataLoaderIter -> _SingleProcessDataLoaderIter (pytorch >=1.4 ) 
# from torch.utils.data.dataloader import _BaseDataLoaderIter #new 

# from torch.utils.data.dataloader import ExceptionWrapper
# from torch.utils.data.dataloader import _use_shared_memory
# from torch.utils.data.dataloader import _pin_memory_loop # _worker_manager_loop -> _pin_memory_loop
# from torch.utils.data.dataloader import numpy_type_map
from torch.utils.data.dataloader import default_collate
# from torch.utils.data.dataloader import pin_memory_batch
# from torch.utils.data.dataloader import _SIGCHLD_handler_set
# from torch.utils.data.dataloader import _set_SIGCHLD_handler

# _use_shared_memory = False #new 

if sys.version_info[0] == 2:
    import Queue as queue
else:
    import queue

def _ms_loop(dataset, index_queue, data_queue, collate_fn, scale, seed, init_fn, worker_id):
    global _use_shared_memory
    _use_shared_memory = True
    _set_worker_signal_handlers()

    torch.set_num_threads(1)
    torch.manual_seed(seed)
    while True:
        r = index_queue.get()
        if r is None:
            break
        idx, batch_indices = r
        try:
            idx_scale = 0
            if len(scale) > 1 and dataset.train:
                idx_scale = random.randrange(0, len(scale))
                dataset.set_scale(idx_scale)

            samples = collate_fn([dataset[i] for i in batch_indices])
            samples.append(idx_scale)

        except Exception:
            data_queue.put((idx, _utils.ExceptionWrapper(sys.exc_info()))) # (idx, ExceptionWrapper(sys.exc_info())) -> (idx, _utils.ExceptionWrapper(sys.exc_info()))
        else:
            data_queue.put((idx, samples))

class _MSDataLoaderIter(_SingleProcessDataLoaderIter): #_DataLoaderIter -> _SingleProcessDataLoaderIter
    def __init__(self, loader):
        self.dataset = loader.dataset
        self.scale = loader.scale
        self.collate_fn = loader.collate_fn
        self.batch_sampler = loader.batch_sampler
        self.num_workers = loader.num_workers
        self.pin_memory = loader.pin_memory and torch.cuda.is_available()
        self.timeout = loader.timeout
        self.done_event = threading.Event()

        self.sample_iter = iter(self.batch_sampler)

        if self.num_workers > 0:
            self.worker_init_fn = loader.worker_init_fn
            self.index_queues = [
                multiprocessing.Queue() for _ in range(self.num_workers)
            ]
            self.worker_queue_idx = 0
            self.worker_result_queue = multiprocessing.Queue() #SimpleQueue -> Queue
            self.batches_outstanding = 0
            self.worker_pids_set = False
            self.shutdown = False
            self.send_idx = 0
            self.rcvd_idx = 0
            self.reorder_dict = {}

            base_seed = torch.LongTensor(1).random_()[0]
            self.workers = [
                multiprocessing.Process(
                    target=_ms_loop,
                    args=(
                        self.dataset,
                        self.index_queues[i],
                        self.worker_result_queue,
                        self.collate_fn,
                        self.scale,
                        base_seed + i,
                        self.worker_init_fn,
                        i
                    )
                )
                for i in range(self.num_workers)]

            if self.pin_memory or self.timeout > 0:
                self.data_queue = queue.Queue()
                if self.pin_memory:
                    maybe_device_id = torch.cuda.current_device()
                else:
                    # do not initialize cuda context if not necessary
                    maybe_device_id = None
                self.pin_memory_thread = threading.Thread( # worker_manager_thread -> pin_memory_thread 
                    target=_utils.pin_memory._pin_memory_loop, # _worker_manager_loop -> _pin_memory_loop -> _utils.pin_memory._pin_memory_loop
                    args=(self.worker_result_queue, self.data_queue, maybe_device_id, self.done_event)) #self.worker_result_queue, self.data_queue, self.done_event, self.pin_memory, maybe_device_id) -> self.worker_result_queue, self.data_queue, maybe_device_id, self.done_event) 
                self.pin_memory_thread.daemon = True # self.worker_manager_thread -> self.pin_memory_thread
                self.pin_memory_thread.start() # self.worker_manager_thread -> self.pin_memory_thread
            else:
                self.data_queue = self.worker_result_queue

            for w in self.workers:
                w.daemon = True  # ensure that the worker exits on process exit
                w.start()

            _utils.signal_handling._set_worker_pids(id(self), tuple(w.pid for w in self.workers)) #_update_worker_pids(id(self), tuple(w.pid for w in self.workers)) -> _utils.signal_handling._set_worker_pids(id(self), tuple(w.pid for w in self.workers))  
            _utils.signal_handling._set_SIGCHLD_handler() # _set_SIGCHLD_handler() -> _utils.signal_handling._set_SIGCHLD_handler()
            self.worker_pids_set = True

            # prime the prefetch loop
            for _ in range(2 * self.num_workers):
                self._put_indices()

# class MSDataLoader(DataLoader): #DataLoader 
#     def __init__(
#         self, args, dataset, batch_size=1, shuffle=False,
#         sampler=None, batch_sampler=None,
#         collate_fn=default_collate, pin_memory=False, drop_last=False,
#         timeout=0, worker_init_fn=None):
        
#         super(MSDataLoader, self).__init__(
#             dataset, batch_size=batch_size, shuffle=shuffle,
#             sampler=sampler, batch_sampler=batch_sampler,
#             num_workers=args.n_threads, collate_fn=collate_fn,
#             pin_memory=pin_memory, drop_last=drop_last,
#             timeout=timeout, worker_init_fn=worker_init_fn)

#         self.scale = args.scale

#     def __iter__(self):
#         return _MSDataLoaderIter(self)


class MSDataLoader(DataLoader):
    def __iter__(self) -> '_BaseDataLoaderIter':
        if self.num_workers == 0:
            return _SingleProcessDataLoaderIter(self)
        else:
            return _MultiProcessingDataLoaderIter(self)

class _SingleProcessDataLoaderIter(_BaseDataLoaderIter):
    def __init__(self, loader):
        super(_SingleProcessDataLoaderIter, self).__init__(loader)
        assert self._timeout == 0
        assert self._num_workers == 0

        self._dataset_fetcher = _DatasetKind.create_fetcher(
            self._dataset_kind, self._dataset, self._auto_collation, self._collate_fn, self._drop_last)

# class _BaseDataLoaderIter(object):
#     def __init__(self, loader):
#         self._dataset = loader.dataset
#         self._dataset_kind = loader._dataset_kind
#         self._IterableDataset_len_called = loader._IterableDataset_len_called
#         self._auto_collation = loader._auto_collation
#         self._drop_last = loader.drop_last
#         self._index_sampler = loader._index_sampler
#         self._num_workers = loader.num_workers
#         self._pin_memory = loader.pin_memory and torch.cuda.is_available()
#         self._timeout = loader.timeout
#         self._collate_fn = loader.collate_fn
#         self._sampler_iter = iter(self._index_sampler)
#         self._base_seed = torch.empty((), dtype=torch.int64).random_().item()
#         self._num_yielded = 0