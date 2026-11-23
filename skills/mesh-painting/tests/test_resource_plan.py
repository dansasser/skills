"""Dimensions and auxiliary work are bounded before allocation."""
import unittest
from unittest.mock import patch
import numpy as np
import common  # noqa: F401
from meshpaint import limits


class DimensionPlan(unittest.TestCase):
    def test_both_delivery_and_working_areas_are_checked(self):
        from meshpaint.resources import JobPlan
        budget = limits.Budget('draft')
        with self.assertRaises(limits.LimitExceeded):
            JobPlan.resolve((100000, 100000), .001, budget=budget)
        with self.assertRaises(limits.LimitExceeded):
            JobPlan.resolve((1000, 1000), 4, budget=budget)
        p = JobPlan.resolve((1000, 1600), 2.4)
        self.assertEqual(p.working_size, (2400, 3840))
        self.assertEqual(p.delivery_size, (1000, 1600))
        self.assertEqual(p.pixel_area, 9_216_000)
        self.assertEqual(p.camera_scale, 1.5)

    def test_dimension_and_scale_validation(self):
        from meshpaint.resources import JobPlan
        for size, scale in [((1, 8), 1), ((True, 32), 1), ((32.5, 32), 1),
                            ((32, 32), float('nan')), ((32, 32), 0), ((32, 32), float('inf'))]:
            with self.subTest(size=size, scale=scale), self.assertRaises(ValueError):
                JobPlan.resolve(size, scale)

    def test_shadow_map_extreme_aspect_has_a_bounded_area(self):
        from meshpaint.resources import fit_map
        p = fit_map('sun', 4096, 4096 * 1_000_000, max_pixels=2_304_000, max_dimension=8192)
        self.assertLessEqual(p.width*p.height, 2_304_000)
        self.assertLessEqual(max(p.width, p.height), 8192)
        self.assertGreater(min(p.width, p.height), 0)
        self.assertTrue(p.reduced)

    def test_aggregate_map_and_output_reserve_are_counted(self):
        from meshpaint.resources import JobPlan, ResourcePlan
        from meshpaint.scene import Scene, Light
        from meshpaint.geometry import Camera
        sc = Scene(None, Camera((0, 0, 2), 0, 0), lambda g: None, sea=False,
                   lights=[Light((0, 0, 3)) for _ in range(1000)])
        job = JobPlan.resolve((32, 32), 1)
        with patch.object(limits, 'process_memory', return_value={'address_space_kb': 100000}):
            p = ResourcePlan.for_scene(sc, job, 3, 1, budget=limits.Budget('draft'))
        self.assertLessEqual(p.estimated_peak_bytes, p.ceiling_bytes)
        self.assertGreater(p.output_reserve_bytes, 0)
        self.assertTrue(all(m.pixel_limit<=limits.LEVELS['draft'].pixels for m in [p.sun_map,*p.point_maps]))
        self.assertLessEqual(sum(m.width*m.height for m in p.point_maps)+p.sun_map.width*p.sun_map.height,
                             p.map_pixel_budget)

    def test_actual_extreme_shadow_shape_is_admitted_before_raster(self):
        from meshpaint.geometry import SunShadow
        from meshpaint.resources import fit_map
        from meshpaint import native
        V=np.array([(0,0,0),(.001,0,0),(0,100000,1)],np.float32)
        F=np.array([[0,1,2]],np.int32)
        requests=[]
        def raster(x,y,q,f,m,w,h,**kwargs):
            requests.append((w,h))
            return None,None,None,np.zeros((h,w),np.float32)
        with patch.object(native,'raster',side_effect=raster):
            sh=SunShadow(V,F,np.array([0.,0.,1.],np.float32),plan=fit_map('sun',4096,4096,max_pixels=10000,max_dimension=256))
        self.assertTrue(sh.plan.reduced)
        self.assertLessEqual(requests[0][0]*requests[0][1],10000)
        self.assertLessEqual(max(requests[0]),256)

    def test_sparse_large_material_id_does_not_allocate_to_max_id(self):
        from meshpaint import mesh,pipeline
        from meshpaint.geometry import Camera
        from meshpaint.scene import Scene
        ms=mesh.MeshSet();ms.add([(-2,0,0),(2,0,0),(0,0,3)],[[0,1,2]],2**31-1)
        def shade(g):
            return np.full((len(g.x),3),.3,np.float32),np.zeros(len(g.x),np.float32),0.
        sc=Scene(ms,Camera((0,-6,2),0,0),shade,sea=False,size=(32,32),regions={2**31-1:127,7:2})
        with patch.object(limits,'_active',limits.Budget()):
            result=pipeline.render(sc,1,lambda _:None)
        self.assertIn(127,np.unique(result['regions']))
        self.assertTrue(np.isfinite(result['target']).all())

    def test_unused_region_declarations_do_not_consume_paint_mask_admission(self):
        from meshpaint.resources import JobPlan,ResourcePlan
        from meshpaint.scene import Scene
        from meshpaint.geometry import Camera
        sc=Scene(None,Camera((0,0,2),0,0),lambda g:None,sea=False,
                 regions={m:m-1 for m in range(3,129)})
        budget=limits.Budget('draft')
        job=JobPlan.resolve((1920,1200),1,budget=budget)
        with patch.object(limits,'process_memory',return_value={'address_space_kb':100000}):
            plan=ResourcePlan.for_scene(sc,job,3,1,budget=budget,used_materials=[2])
            with self.assertRaises(limits.LimitExceeded):
                ResourcePlan.for_scene(sc,job,3,1,budget=budget)
        self.assertLessEqual(plan.estimated_peak_bytes,plan.ceiling_bytes)
        self.assertEqual(plan.lifetime_groups['paint'],3*48+32+176*job.pixel_area)


if __name__ == '__main__':
    unittest.main()
