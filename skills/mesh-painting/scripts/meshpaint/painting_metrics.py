"""Fixed-reference multiscale evaluation, bounded independently of canvas size."""
import math

import numpy as np
from scipy.ndimage import gaussian_filter, sobel


class PaintingMetrics:
    def __init__(self, target, regions, evaluation_pixels):
        h, w = regions.shape
        lo, hi = max(1, math.ceil(math.sqrt(h*w/evaluation_pixels))), max(h, w)
        while lo < hi:
            mid = (lo+hi)//2
            if math.ceil(h/mid)*math.ceil(w/mid) <= evaluation_pixels:
                hi = mid
            else:
                lo = mid+1
        self.step = lo
        self.target = np.ascontiguousarray(target[::self.step, ::self.step], np.float32)
        self.regions = regions[::self.step, ::self.step]
        self.low = gaussian_filter(self.target, (1.2, 1.2, 0))
        texture = np.abs(self.target-self.low).mean(axis=2)
        self.texture = float(texture.mean())
        self.edge = self.edges(self.target)
        self.ids, counts = np.unique(regions, return_counts=True)
        self.locations = {int(k): np.flatnonzero(regions.ravel() == k) for k in self.ids}
        self.area = {int(k): int(n)/(h*w) for k, n in zip(self.ids, counts)}
        self.detail = {}
        self.region_texture = {}
        for k in self.ids:
            mask = self.regions == k
            if mask.any():
                values = self.target[mask]
                self.detail[int(k)] = float(values.std(axis=0).mean()+self.edge[mask].mean())
                self.region_texture[int(k)] = float(texture[mask].mean())
            else:
                loc = self.locations[int(k)]
                take = loc[np.linspace(0, len(loc)-1, min(256, len(loc))).astype(int)]
                self.detail[int(k)] = float(target.reshape(-1, 3)[take].std(axis=0).mean())
                self.region_texture[int(k)] = 0.0

    @staticmethod
    def edges(image):
        lum = image @ np.array((.3, .6, .1), np.float32)
        return np.hypot(sobel(lum, 0), sobel(lum, 1)) / 8

    def evaluate(self, canvas, height, target):
        current = canvas[::self.step, ::self.step]
        colour = np.abs(current-self.target).mean(axis=2)
        low = np.abs(gaussian_filter(current, (1.2, 1.2, 0))-self.low).mean(axis=2)
        edge = np.abs(self.edges(current)-self.edge)
        error = .5*colour+.3*low+.2*edge
        coverage = height[::self.step, ::self.step] > .02
        regional, covered, regional_low = {}, {}, {}
        for k in self.ids:
            key = int(k)
            mask = self.regions == k
            if mask.any():
                regional[key] = float(error[mask].mean())
                covered[key] = float(coverage[mask].mean())
                regional_low[key] = float(low[mask].mean())
            else:
                loc = self.locations[key]
                take = loc[np.linspace(0, len(loc)-1, min(256, len(loc))).astype(int)]
                regional[key] = float(np.abs(canvas.reshape(-1, 3)[take]-target.reshape(-1, 3)[take]).mean())
                covered[key] = float((height.ravel()[take] > .02).mean())
                regional_low[key] = regional[key]
        return dict(objective=float(error.mean()), colour=float(colour.mean()), low=float(low.mean()),
                    edge=float(edge.mean()), coverage=float(coverage.mean()), regions=regional,
                    region_coverage=covered, region_low=regional_low), error
