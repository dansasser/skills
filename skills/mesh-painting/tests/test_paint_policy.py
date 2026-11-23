"""Adaptive work follows the image and stays within the admitted work budget."""
import unittest
from unittest.mock import patch

import numpy as np

import common
from meshpaint import limits, paint
from meshpaint.painting_policy import resolve_config, stroke_ceiling


class PolicyContract(unittest.TestCase):
    def setUp(self):
        self.previous = limits.active()
        limits.use()
        self.addCleanup(setattr, limits, '_active', self.previous)

    def test_equal_area_uses_equal_ceiling(self):
        self.assertEqual(stroke_ceiling(1000, 1600), stroke_ceiling(1600, 1000))
        self.assertLess(stroke_ceiling(64, 48), stroke_ceiling(640, 480))
        self.assertEqual(stroke_ceiling(640, 480, 100), 100)
        with self.assertRaises(ValueError):
            stroke_ceiling(640, 480, 3_000_001)

    def test_configuration_rejects_unknown_nonfinite_and_unbounded_values(self):
        for config in ({'unknown': 1}, {'schema_version': 2}, {'ratio': 1},
                       {'batch_size': True}, {'batch_size': 0}, {'target_error': float('nan')},
                       {'max_batches': 0}, {'min_width': -1}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                resolve_config(config)

    def test_legacy_refuses_adaptive_customization(self):
        target = np.full((24, 32, 3), .5, np.float32)
        for kwargs in ({'finish': 'painterly'}, {'config': {}}, {'max_strokes': 10}, {'max_passes': 3}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                paint.paint(target, np.zeros((24, 32), np.int8), {}, .02, 0,
                            np.random.default_rng(1), lambda _: None, **kwargs)

    def render(self, target, **kwargs):
        regions = np.full(target.shape[:2], 2, np.int8)
        image, strokes = paint.paint(target, regions, {}, 1, 0, np.random.default_rng(123),
                                     lambda _: None, grain=0, policy='adaptive-v1', seed=123, **kwargs)
        return image, strokes, limits.active().painting

    def test_repeat_seed_reproduces_pixels_and_decisions(self):
        target = np.full((48, 64, 3), .5, np.float32)
        a, count_a, report_a = self.render(target)
        limits.use()
        b, count_b, report_b = self.render(target)
        np.testing.assert_array_equal(a, b)
        self.assertEqual(count_a, count_b)
        self.assertEqual(report_a['decisions'], report_b['decisions'])
        self.assertIn(report_a['terminal_reason'], ('quality_target', 'diminishing_returns'))

    def test_smooth_target_requires_less_refinement_than_structure(self):
        smooth = np.full((64, 80, 3), .5, np.float32)
        yy, xx = np.indices(smooth.shape[:2])
        detail = np.repeat((.2+.6*((xx//8+yy//8)%2))[..., None], 3, axis=2).astype(np.float32)
        _, smooth_count, _ = self.render(smooth)
        limits.use()
        try:
            _, detail_count, _ = self.render(detail)
        except limits.LimitExceeded:
            # A detailed target may legitimately consume its unchanged area ceiling.
            report = limits.active().painting
            self.assertEqual(report['terminal_reason'], 'stroke_budget')
            detail_count = report['attempted_strokes']
        self.assertLess(smooth_count, detail_count)

    def test_budget_never_generates_an_extra_stroke_and_keeps_checkpoint(self):
        target = np.full((48, 64, 3), .8, np.float32)
        checkpoints = []
        with self.assertRaises(limits.LimitExceeded):
            self.render(target, max_strokes=17, checkpoint=lambda c, p, n: checkpoints.append(n))
        report = limits.active().painting
        self.assertLessEqual(report['attempted_strokes'], 17)
        self.assertEqual(report['terminal_reason'], 'stroke_budget')
        self.assertTrue(checkpoints)
        self.assertLessEqual(max(checkpoints), 17)

    def test_sparse_small_region_receives_a_batch(self):
        target = np.full((64, 80, 3), .5, np.float32)
        regions = np.full((64, 80), 2, np.int8)
        regions[20:30, 32:40] = 127
        target[20:30, 32:40] = .15
        paint.paint(target, regions, {127: 1.0}, 1, 0, np.random.default_rng(123),
                    lambda _: None, grain=0, policy='adaptive-v1', seed=123)
        decisions = limits.active().painting['decisions']
        self.assertIn(127, {d['region'] for d in decisions})

    def test_rejected_batches_consume_budget_and_restore_height(self):
        target = np.full((32, 40, 3), .5, np.float32)
        previous_heights = []
        def damage(canvas, height, *args):
            previous_heights.append(float(height.max()))
            canvas[:] = 1
            height[:] = 999
        with patch.object(paint.native, 'strokes', side_effect=damage):
            try:
                self.render(target, max_strokes=40)
            except limits.LimitExceeded:
                pass
        report = limits.active().painting
        self.assertLessEqual(report['attempted_strokes'], 40)
        self.assertEqual(report['completed_strokes'], 0)
        self.assertTrue(all(not d['accepted'] for d in report['decisions']))
        self.assertGreaterEqual(len(previous_heights), 2)
        self.assertEqual(previous_heights, [0]*len(previous_heights))

    def test_pass_guard_is_distinct_and_keeps_last_whole_batch(self):
        checkpoints = []
        with self.assertRaises(limits.LimitExceeded):
            self.render(np.full((48, 64, 3), .8, np.float32), max_passes=1,
                        checkpoint=lambda c, p, n: checkpoints.append(n))
        self.assertEqual(limits.active().painting['terminal_reason'], 'pass_guard')
        self.assertEqual(len(limits.active().painting['decisions']), 1)
        self.assertEqual(checkpoints[-1], limits.active().painting['completed_strokes'])

    def test_configured_batch_size_is_a_real_upper_bound(self):
        with self.assertRaises(limits.LimitExceeded):
            self.render(np.full((48, 64, 3), .8, np.float32), config={'batch_size': 1}, max_passes=1)
        self.assertEqual(limits.active().painting['attempted_strokes'], 1)

    def test_native_memory_failure_is_reported_as_memory(self):
        with patch.object(paint.native, 'strokes', side_effect=MemoryError('injected allocation')):
            with self.assertRaises(MemoryError):
                self.render(np.full((48, 64, 3), .8, np.float32))
        self.assertEqual(limits.active().painting['terminal_reason'], 'memory')

    def test_thin_image_has_no_permanently_unreachable_pixel_anchors(self):
        target = np.full((16, 20000, 3), .5, np.float32)
        try:
            _, _, report = self.render(target)
        except limits.LimitExceeded:
            report = limits.active().painting
            self.assertEqual(report['terminal_reason'], 'stroke_budget')
            self.assertLessEqual(report['attempted_strokes'], report['admitted_strokes'])
        self.assertGreaterEqual(report['final_metrics']['coverage'], .97)
        self.assertLess(report['final_metrics']['objective'], .025)


class FixedMetrics(unittest.TestCase):
    def test_actual_sample_shape_respects_cap_for_thin_images(self):
        from meshpaint.painting_metrics import PaintingMetrics
        target = np.full((16, 16384, 3), .5, np.float32)
        regions = np.zeros(target.shape[:2], np.int8)
        metric = PaintingMetrics(target, regions, 64)
        self.assertLessEqual(metric.target.shape[0]*metric.target.shape[1], 64)

    def test_wide_blur_on_thin_canvas_preserves_constant_reference(self):
        from meshpaint.util import blur
        target = np.ones((16, 20000), np.float32)
        result = blur(target, 62.5)
        self.assertEqual(result.shape, target.shape)
        self.assertTrue(np.isfinite(result).all())
        np.testing.assert_allclose(result, target, atol=1e-6)


if __name__ == '__main__':
    unittest.main()
