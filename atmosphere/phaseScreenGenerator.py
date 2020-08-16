import ceo
import json
from subprocess import call
import os

for SEED in [1234,]:
    L0   = 25.0
    filepath = "/mnt/bins/"
    filename = filepath+"gmtAtmosphereL0%d_%d"%(L0,SEED)
    atm_prms = {'r0':0.15,'L0':L0,'L':26.0,'NXY_PUPIL':346,'fov':20*ceo.constants.ARCMIN2RAD,
               'duration':15,'N_DURATION':20,'filename':filename+'.bin',
                'SEED':SEED}

    with open(filename+'.json', 'w') as outfile:
        print ">>> "+outfile.name
        json.dump(atm_prms, outfile, sort_keys = False, indent = 4, ensure_ascii=False)

    atm = ceo.GmtAtmosphere(**atm_prms)

    os.system("zip -0 -j "+filename+".zip "+filename+".json "+filename+".bin")
    os.system("aws s3 cp "+filename+".zip s3://gmto.rconan/PhaseScreens/")
    os.system("rm "+filename+".zip "+filename+".json "+filename+".bin")
