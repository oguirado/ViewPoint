"""Explicit analytic qualification fixture. These are not generated assets."""
from copy import deepcopy


def fall_fixture(source):
    """[TARGET] SI box, free-fall then contact; tolerances require local measurement."""
    sha = source["sha256"]
    identity = [1000000, 0, 0, 0]
    def geom(name, shape, data):
        return {"id":name,"shape":shape,"data":data,"position_um":[0,0,0],
                "quaternion_ppm":identity[:],"source_sha256":sha}
    floor = {"id":"floor","dynamic":False,"position_um":[0,0,0],"quaternion_ppm":identity[:],
             "velocity_um_s":[0,0,0],"angular_velocity_urad_s":[0,0,0],"inertial":None,
             "colliders":[geom("ground","plane",{"half_extents_um":[2000000,2000000]})]}
    # A unit-mass cube with side length 0.2 m: I = m*(a*a+a*a)/12.
    box = {"id":"box","dynamic":True,"position_um":[0,0,1000000],"quaternion_ppm":identity[:],
           "velocity_um_s":[0,0,0],"angular_velocity_urad_s":[0,0,0],
           "inertial":{"mass_ug":10**9,"com_um":[0,0,0],
                       "tensor_ug_um2":[6666666666666666667]*3+[0,0,0],
                       "provenance":{"method":"analytic_fixture","source_sha256":sha}},
           "colliders":[geom("box_geom","box",{"half_extents_um":[100000]*3})]}
    scene = {"schema":"af.physics-scene/v1","asset_id":"analytic_drop","source":deepcopy(source),
             "units":{"length":"um","mass":"ug","inertia":"ug_um2"},"bodies":[floor,box]}
    # [MEASURED] Native qualification uses finer integration and stiffer contact.
    # The previous 1 ms / 20 ms contact setting breaches the unchanged 5 mm guard.
    protocol = {"schema":"af.physics-protocol/v1","gravity_um_s2":[0,0,-9810000],
                "expected_gravity_um_s2":[0,0,-9810000],"timestep_us":500,"steps":2000,
                "timeout_s":30,"normalization_length_um":1000000,"max_trajectory_error_ppm":10000,
                "inertia_tolerance_ppm":1000,"solver_iterations":100,"solver_tolerance_ppb":1,
                "contact":{"max_penetration_um":5000,"initial_penetration_um":0,"max_contacts":16,
                           "require_contact":True,"friction_ppm_um":[500000,5000,100],
                           "timeconst_us":2000,"dampratio_ppm":1000000},
                "free_fall":{"body_id":"box","steps":200,"tolerance_um":1000}}
    return scene, protocol
