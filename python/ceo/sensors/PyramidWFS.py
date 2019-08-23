from ceo.tools import ascupy
from ceo.pyramid import Pyramid
import numpy as np
import cupy as cp
from scipy.ndimage import center_of_mass

class PyramidWFS(Pyramid):
    def __init__(self, N_SIDE_LENSLET, N_PX_LENSLET, modulation=0.0, N_GS=1, throughput=1.0):
        Pyramid.__init__(self)
        self._ccd_frame = ascupy(self.camera.frame)
        self._SUBAP_NORM = 'MEAN_FLUX_PER_SUBAP'
        self.camera.photoelectron_gain = throughput
	
    def calibrate(self, src, calib_modulation=10.0, calib_modulation_sampling=64, percent_extra_subaps=0.0, thr=0.0):
        """
        Perform the following calibration tasks:
        1) Acquire a CCD frame using high modulation (default: 10 lambda/D);
        2) Estimate center of the four sub-pupil images;
        3) Calibrate an initial pupil registration assuming a circular pupil.
        4) Refines pupil registration by selecting only sub-apertures with flux above threshold thr.
        5) Stores pupil registration in self._indpup
        6) Computes and stores the reference slope null vector for a flat WF

        Parameters
        ----------
        src : Source
             The Source object used for Pyramid sensing
        gmt : GMT_MX
             The GMT object
        calib_modulation: modulation radius applied during calibration (default 10 lambda/D).
        percent_extra_subaps: percent of extra subapertures across the pupil for initial pupil registration (default: 0).
        thr : Threshold for pupil registration refinement: select only SAs with flux percentage above thr.
        """

        #-> Acquire CCD frame applying high modulation:
        self.reset()
        cl_modulation = self.modulation # keep track of selected modulation radius
        cl_modulation_sampling = self.modulation_sampling
        self.modulation = calib_modulation
        self.modulation_sampling = calib_modulation_sampling
        self.propagate(src)
        ccd_frame = self._ccd_frame.get()
        self.modulation = cl_modulation
        self.modulation_sampling = cl_modulation_sampling

        #-> Find center of four sup-pupil images:
        nx, ny = ccd_frame.shape
        x = np.linspace(0, nx-1, nx)
        y = np.linspace(0, ny-1, ny)
        xx, yy = np.meshgrid(x, y)

        mqt1 = np.logical_and(xx<(nx/2), yy<(ny/2)) # First quadrant (lower left)
        mqt2 = np.logical_and(xx>(nx/2), yy<(ny/2)) # Second quadrant (lower right)
        mqt3 = np.logical_and(xx<(nx/2), yy>(ny/2)) # Third quadrant (upper left)
        mqt4 = np.logical_and(xx>(nx/2), yy>(ny/2)) # Fourth quadrant (upper right)

        label = np.zeros((nx,ny)) # labels needed for ndimage.center_of_mass
        label[mqt1] = 1
        label[mqt2] = 2
        label[mqt3] = 3
        label[mqt4] = 4

        centers = center_of_mass(ccd_frame, labels=label, index=[1,2,3,4])
        print("Center of subpupil images (pix):")
        print(np.array_str(np.array(centers), precision=1), end='\n')

        #-> Circular pupil registration
        n_sub = self.N_SIDE_LENSLET + round(self.N_SIDE_LENSLET*percent_extra_subaps/100)
        print("Number of SAs across pupil: %d"%n_sub, end="\r", flush=True)

        indpup = []  # list of pupil index vectors
        n_sspp = []
        for this_pup in range(4):
            indpup.append( (xx-centers[this_pup][0])**2 + (yy-centers[this_pup][1])**2 <= round(n_sub/2)**2 )
            n_sspp.append( np.sum(indpup[this_pup]) )
        n_sspp = np.unique(n_sspp)

        if n_sspp.size > 1:
            print('Error!! number of valid SAs per sub-pupil are different!')
            print(np.array_str(np.array(n_sspp)))
            return	

        #-> Pupil registration refinement based on SA flux thresholding
        if thr > 0:

            # Compute the flux per SA 
            flux = np.zeros(n_sspp)
            for subpup in indpup:
                flux += ccd_frame[subpup]

            meanflux = np.mean(flux) 
            fluxthr = meanflux*thr
            thridx = flux > fluxthr
            n_sspp1 = np.sum(thridx)
            print("->     Number of valid SAs: %d"%n_sspp1, flush=True)

            #indpup1 = [np.copy(subpup) for subpup in indpup]
            for subpup in indpup:
                subpup[subpup] *= thridx

        #-> Save pupil registration (GPU memory)		
        self._indpup = [cp.asarray(subpup) for subpup in indpup]
        self.n_sspp = int(cp.sum(self._indpup[0])) # number of valid SAs

        #-> Compute reference vector
        self.analyze(src)
        self._ref_measurement = self._measurement.copy()


    @property
    def indpup(self):
        """
        Pupil regitration: List containing the valid sub-aperture maps for each of the four sub-pupil images. 
        """
        return [cp.asnumpy(subpup) for subpup in self._indpup]

    @property
    def ccd_frame(self):
        return self._ccd_frame.get()

    @property
    def signal_normalization(self):
        return self._SUBAP_NORM
    @signal_normalization.setter
    def signal_normalization(self, value):
        assert value == 'QUADCELL' or value == 'MEAN_FLUX_PER_SUBAP', 'Normalization supported: "QUADCELL", "MEAN_FLUX_PER_SUBAP"' 
        self._SUBAP_NORM = value
	
    def process(self):
        """
        Computes the measurement vector from CCD frame.
        """
        # Flux computation for normalization factors
        flux_per_SA = cp.zeros(self.n_sspp)
        for subpup in self._indpup:
            flux_per_SA += self._ccd_frame[subpup]
        tot_flux = cp.sum(flux_per_SA)
    
        # If the frame has some photons, compute the signals...
        if tot_flux > 0:

            # Choose the signal normalization factor:
            if self._SUBAP_NORM == 'QUADCELL':
                norm_fact = flux_per_SA       # flux on each SA
            elif self._SUBAP_NORM == 'MEAN_FLUX_PER_SUBAP':
                norm_fact = tot_flux / self.n_sspp # mean flux per SA
    
            # Compute the signals
            sx = (self._ccd_frame[self._indpup[3]]+self._ccd_frame[self._indpup[2]]-
                  self._ccd_frame[self._indpup[1]]-self._ccd_frame[self._indpup[0]]) / norm_fact  

            sy = (self._ccd_frame[self._indpup[1]]+self._ccd_frame[self._indpup[3]]-
                  self._ccd_frame[self._indpup[0]]-self._ccd_frame[self._indpup[2]]) / norm_fact 

        else:
            # If the frame has no photons, provide a zero slope vector!
            sx = cp.zeros(self.n_sspp)
            sy = cp.zeros(self.n_sspp)

        self._measurement = [sx,sy]

    @property
    def Data(self):
        return self.get_measurement()

    def get_measurement(self, out_format='vector'):
        """
        Returns the measurement vector minus reference vector.
        Parameters:
          out_format: if "vector" return a 1D vector (default). If "list" return [sx,sy].
        """
        assert out_format == 'vector' or out_format == 'list', 'output format supported: "vector", "list [sx,sy]"'
        meas = [m - n for (m,n) in zip(self._measurement, self._ref_measurement)]
        if out_format == 'vector':
            return cp.asnumpy(cp.concatenate(meas))
        else:
            return cp.asnumpy(meas)

    def get_ref_measurement(self, out_format='vector'):
        """
        Returns the reference measurement vector.
        Parameters:
          out_format: if "vector" return a 1D vector (default). If "list" return [sx,sy].
        """
        assert out_format == 'vector' or out_format == 'list', 'output format supported: "vector", "list [sx,sy]"'
        if out_format == 'vector':
            return cp.asnumpy(cp.concatenate(self._ref_measurement))
        else:
            return cp.asnumpy(self._ref_measurement)

    def get_sx(self):
        """
        Returns Sx in vector format.
        """
        return self.get_measurement(out_format='list')[0] 

    def get_sy(self):
        """
        Returns Sy in vector format.
        """
        return self.get_measurement(out_format='list')[1]

    def get_sx2d(self, this_sx=None):
        """
        Returns Sx in 2D format.
        """
        if this_sx is None:
           this_sx = self.get_sx()
 
        #sx2d = np.full((self.camera.N_PX_FRAME,self.camera.N_PX_FRAME), np.nan)
        sx2d = np.zeros((self.camera.N_PX_FRAME,self.camera.N_PX_FRAME))
        sx2d[self.indpup[0]] = this_sx
        sx2d = sx2d[0:int(self.camera.N_PX_FRAME/2),0:int(self.camera.N_PX_FRAME/2)]
        return sx2d
        
    def get_sy2d(self, this_sy=None):
        """
        Returns Sy in 2D format.
        """
        if this_sy is None:
           this_sy = self.get_sy()

        #sy2d = np.full((self.camera.N_PX_FRAME,self.camera.N_PX_FRAME), np.nan)
        sy2d = np.zeros((self.camera.N_PX_FRAME,self.camera.N_PX_FRAME))
        sy2d[self.indpup[0]] = this_sy
        sy2d = sy2d[0:int(self.camera.N_PX_FRAME/2),0:int(self.camera.N_PX_FRAME/2)]
        return sy2d

    def get_measurement_size(self):
        """
        Returns the size of the measurement vector.
        """
        return self.n_sspp * 2

    def measurement_rms(self):
        """
        Returns the slope RMS (Sx and Sy).
        """
        return (np.std(self.get_sx()), np.std(self.get_sy()))

    def reset(self):
        """
        Resets the detector frame.
        """
        self.camera.reset()

    def analyze(self, src):
        """
        Propagates the guide star to the Pyramid detector (noiseless) and processes the frame

        Parameters
        ----------
        src : Source
            The pyramid's guide star object
        """
        self.reset()
        self.propagate(src)
        self.process()


