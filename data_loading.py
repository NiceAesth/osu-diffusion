import math
import os.path
import random
import pickle
from datetime import timedelta

from slider import Position
from slider.beatmap import Beatmap, HitObject, Slider, Spinner
import torch
from slider.curve import Linear, Catmull, Perfect, MultiBezier
from torch.utils.data import IterableDataset, DataLoader

from positional_embedding import timestep_embedding, position_sequence_embedding


playfield_size = torch.tensor((512, 384))
context_size = 14 + 128


def create_datapoint(time: timedelta, pos: Position, datatype):
    pos_enc = torch.tensor(pos) / playfield_size
    type_enc = torch.zeros(15)
    type_enc[0] = time.total_seconds() * 10
    type_enc[datatype + 1] = 1

    return torch.concatenate([pos_enc, type_enc], 0)


def repeat_type(repeat):
    if repeat < 4:
        return repeat - 1
    elif repeat % 2 == 0:
        return 3
    else:
        return 4


def append_control_points(datapoints, ho: Slider, datatype):
    control_point_count = len(ho.curve.points)
    duration = ho.end_time - ho.time

    for i in range(1, control_point_count - 1):
        time = ho.time + i / (control_point_count - 1) * duration
        pos = ho.curve.points[i]
        datapoints.append(create_datapoint(time, pos, datatype))


def get_data(ho: HitObject):
    if isinstance(ho, Slider) and len(ho.curve.points) < 100:
        datapoints = [create_datapoint(ho.time, ho.position, 3)]

        if isinstance(ho.curve, Linear):
            append_control_points(datapoints, ho, 7)
        elif isinstance(ho.curve, Catmull):
            append_control_points(datapoints, ho, 6)
        elif isinstance(ho.curve, Perfect):
            append_control_points(datapoints, ho, 5)
        elif isinstance(ho.curve, MultiBezier):
            control_point_count = len(ho.curve.points)
            duration = ho.end_time - ho.time

            for i in range(1, control_point_count - 1):
                time = ho.time + i / (control_point_count - 1) * duration
                pos = ho.curve.points[i]

                if pos == ho.curve.points[i + 1]:
                    datapoints.append(create_datapoint(time, pos, 7))
                elif pos != ho.curve.points[i - 1]:
                    datapoints.append(create_datapoint(time, pos, 4))

        datapoints.append(create_datapoint(ho.end_time, ho.curve.points[-1], 8))
        datapoints.append(create_datapoint(ho.end_time, ho.curve(1), 9 + repeat_type(ho.repeat)))

        return torch.stack(datapoints, 0)

    if isinstance(ho, Spinner):
        return torch.stack((create_datapoint(ho.time, ho.position, 1), create_datapoint(ho.end_time, ho.position, 2)), 0)

    return create_datapoint(ho.time, ho.position, 0).unsqueeze(0)


def beatmap_to_sequence(beatmap):
    # Get the hit objects
    hit_objects = beatmap.hit_objects(stacking=False)
    sequence = torch.concatenate([get_data(ho) for ho in hit_objects], 0)

    return sequence


class BeatmapDatasetIterable:
    def __init__(self, beatmap_files, beatmap_idx, seq_len, stride):
        self.beatmap_files = beatmap_files
        self.beatmap_idx = beatmap_idx
        self.seq_len = seq_len
        self.stride = stride
        self.index = 0
        self.current_idx = 0
        self.current_seq_x = None
        self.current_seq_y = None
        self.seq_index = 0

    def __iter__(self):
        return self

    def __next__(self):
        while self.current_seq_x is None or self.seq_index + self.seq_len > len(self.current_seq_x):
            if self.index >= len(self.beatmap_files):
                raise StopIteration

            # Load the beatmap from file
            beatmap_path = self.beatmap_files[self.index]
            beatmap = Beatmap.from_path(beatmap_path)

            self.current_idx = self.beatmap_idx[beatmap.beatmap_id]

            seq_no_embed = beatmap_to_sequence(beatmap)
            self.current_seq_x = seq_no_embed[:, :2]
            self.current_seq_y = torch.concatenate(
                [
                    timestep_embedding(seq_no_embed[:, 2], 128, 36000),
                    seq_no_embed[:, 3:]
                ], 1)

            self.seq_index = 0
            self.index += 1

        # Return the preprocessed hit objects as a sequence of overlapping windows
        x = self.current_seq_x[self.seq_index:self.seq_index + self.seq_len]
        y = self.current_seq_y[self.seq_index:self.seq_index + self.seq_len]
        self.seq_index += self.stride
        return x, y, self.current_idx


class BeatmapDataset(IterableDataset):
    def __init__(self, dataset_path, beatmap_idx, start, end, seq_len, stride=1, shuffle=False):
        super(BeatmapDataset).__init__()
        self.dataset_path = dataset_path
        self.beatmap_idx = beatmap_idx
        self.start = start
        self.end = end
        self.seq_len = seq_len
        self.stride = stride
        self.shuffle = shuffle

    def _get_beatmap_files(self):
        # Get a list of all beatmap files in the dataset path in the track index range between start and end
        beatmap_files = []
        track_names = ["Track" + str(i).zfill(5) for i in range(self.start, self.end)]
        for track_name in track_names:
            for beatmap_file in os.listdir(os.path.join(self.dataset_path, track_name, "beatmaps")):
                beatmap_files.append(os.path.join(self.dataset_path, track_name, "beatmaps", beatmap_file))

        return beatmap_files

    def __iter__(self):
        beatmap_files = self._get_beatmap_files()

        if self.shuffle:
            random.shuffle(beatmap_files)

        return BeatmapDatasetIterable(beatmap_files, self.beatmap_idx, self.seq_len, self.stride)


# Define a `worker_init_fn` that configures each dataset copy differently
def worker_init_fn(worker_id):
    worker_info = torch.utils.data.get_worker_info()
    dataset = worker_info.dataset  # the dataset copy in this worker process
    overall_start = dataset.start
    overall_end = dataset.end
    # configure the dataset to only process the split workload
    per_worker = int(math.ceil((overall_end - overall_start) / float(worker_info.num_workers)))
    dataset.start = overall_start + worker_id * per_worker
    dataset.end = min(dataset.start + per_worker, overall_end)


def get_processed_data_loader(dataset_path, start, end, seq_len, stride, batch_size, num_workers=0, shuffle=False, pin_memory=False, drop_last=False):
    with open('beatmap_idx.pickle', 'rb') as handle:
        beatmap_idx = pickle.load(handle)

    dataset = BeatmapDataset(
        dataset_path=dataset_path,
        beatmap_idx=beatmap_idx,
        start=start,
        end=end,
        seq_len=seq_len,
        stride=stride,
        shuffle=shuffle
    )
    dataloader = DataLoader(
        dataset,
        batch_size=batch_size,
        worker_init_fn=worker_init_fn,
        num_workers=num_workers,
        pin_memory=pin_memory,
        drop_last=drop_last
    )

    return dataloader


if __name__ == '__main__':
    seq_len = 25
    batch_size = 1  # Set the desired batch size
    dataloader = get_processed_data_loader(
        "D:\\Osu! Dingen\\Beatmap ML Datasets\\ORS10548",
        0,
        10548,
        seq_len,
        16,
        batch_size,
        0,
        False
    )

    import matplotlib.pyplot as plt
    for batch in dataloader:
        print(batch[0].shape, batch[1].shape, batch[2].shape)
        batch_pos_emb = position_sequence_embedding(batch[0] * playfield_size, 128)
        print(batch_pos_emb.shape)

        for j in range(batch_size):
            fig, axs = plt.subplots(2, figsize=(10, 5))
            axs[0].imshow(batch_pos_emb[j])
            axs[1].imshow(batch[1][j])
            print(batch[2][j])
            plt.show()
        break

    # import time
    # import tqdm
    # count = 0
    # start = time.time()
    # for f in tqdm.tqdm(dataloader, total=76200, smoothing=0.1):
    #     count += 1
    #     # print(f"\r{count}, {count / (time.time() - start)} per second, beatmap index {torch.max(f[1])}", end='')
