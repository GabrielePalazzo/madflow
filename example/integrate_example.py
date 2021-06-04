"""
    Example script to integrate a given matrix element generated with the pyout madgraph plugin
    In order to run this script, vegasflow needs to be installed

    This script is WIP and is not autogenerated with the rest of the pyout output, to run first generate
    some process with madraph

    ```
        ~$ mg5_aMC
        MG5_aMC>generate g g > t t~
        MG5_aMC>output pyout vegasflow_example
    ```

    which will generate a vegasflow_example folder with all the required files.
    Link that folder to this script in the first line below (`folder`).
"""

import os, argparse
from pathlib import Path

# os.environ["CUDA_VISIBLE_DEVICES"] = ""
import numpy as np
from time import time as tm
from vegasflow import VegasFlow, float_me, run_eager, int_me
#run_eager(True)
from vegasflow.utils import consume_array_into_indices
from pdfflow import mkPDF
from pdfflow.functions import _condition_to_idx
from pdfflow.configflow import fzero, fone, DTYPE

from madflow.lhe_writer import LheWriter

import tensorflow as tf

COM_SQRTS = 7e3
PDF = mkPDF("NNPDF31_nnlo_as_0118/0")
Q2 = pow(91.46, 2)
TOP_MASS = 173.0

costhmax = fone
costhmin = float_me(-1.0) * fone
phimin = fzero
phimax = float_me(2.0 * np.pi)

######### Import the matrix elements and the necessary models
all_matrices_flow = []
model = None
base_model = "models/sm"
# This piece of code loops over all matrix_XXXX.py files in the folder generated by the pyout script
# and loads them to the all_matrices, together with their corresponding model
# at the end it cleans all paths _assuming_ all necessary modules have been loaded
# during the instantiation of the matrix element this should be safe
import sys
import glob
import copy
import importlib.util
import re

re_name = re.compile("\w{3,}")

################# Phase space
@tf.function
def log_pick(r, valmin, valmax):
    """Get a random value between valmin and valmax
    as given by the random number r (batch_size, 1)
    the outputs are val (batch_size, 1) and jac (batch_size, 1)
    Logarithmic sampling

    Parameters
    ----------
        r: random val
        valmin: minimum value
        valmax: maximum value
    Returns
    -------
        val: chosen random value
        jac: jacobian of the transformation
    """
    ratio_val = valmax / valmin
    val = valmin * tf.pow(ratio_val, r)
    jac = val * tf.math.log(ratio_val)
    return val, jac


def get_x1x2(xarr, shat_min, s_in):
    """Receives two random numbers and return the
    value of the invariant mass of the center of mass
    as well as the jacobian of the x1,x2 -> tau-y transformation
    and the values of x1 and x2.

    The xarr array is of shape (batch_size, 2)
    """
    taumin = shat_min / s_in
    taumax = float_me(1.0)
    # Get tau logarithmically
    tau, wgt = log_pick(xarr[:, 0], taumin, taumax)
    x1 = tf.pow(tau, xarr[:, 1])
    x2 = tau / x1
    wgt *= -1.0 * tf.math.log(tau)
    shat = x1 * x2 * s_in
    return shat, wgt, x1, x2


@tf.function
def pick_within(r, valmin, valmax):
    """Get a random value between valmin and valmax
    as given by the random number r (batch_size, 1)
    the outputs are val (batch_size, 1) and jac (batch_size, 1)

    Linear sampling

    Parameters
    ----------
        r: random val
        valmin: minimum value
        valmax: maximum value
    Returns
    -------
        val: chosen random value
        jac: jacobian of the transformation
    """
    delta_val = valmax - valmin
    val = valmin + r * delta_val
    return val, delta_val


def phasespace_generator(xrand, nparticles):
    """Takes as input an array of nevent x ndim random points and outputs
    an array of momenta (nevents x nparticles x 4)
    """
    shat_min = float_me(4 * TOP_MASS ** 2)
    shat, wgt, x1, x2 = get_x1x2(xrand[:, :2], shat_min, COM_SQRTS ** 2)
    roots = tf.sqrt(shat)
    s1 = float_me(TOP_MASS ** 2)
    s2 = float_me(TOP_MASS ** 2)

    zeros = tf.zeros_like(x1)
    ones = tf.ones_like(x1)

    # Get the energy and momenta of the outgoing particles
    # which in the COM are the same...
    ein = roots / 2.0
    eout = (shat + s1 - s2) / 2.0 / roots
    pout = tf.sqrt(eout ** 2 - TOP_MASS ** 2)

    # Get min and max invariant masses s_inout to sample the angle
    # massless input (so p_in == e_in)
    tmin = s1 - roots * (eout + pout)
    tmax = s1 - roots * (eout - pout)
    t, jac = log_pick(xrand[:, 2], -tmax, -tmin)
    costh = (-t - s1 + 2.0 * ein * eout) / (2.0 * ein * pout)
    wgt *= jac
    sinth = tf.sqrt(1.0 - costh ** 2)
    wgt /= shat * 16.0 * np.pi ** 2

    # The azimuthal angle can be set to 0 because of symmetry around the beam
    wgt *= 2.0 * np.pi
    cosphi = fone
    sinphi = fzero

    # Write down the outging momenta in the COM system
    px = pout * sinth * cosphi
    py = pout * sinth * sinphi
    pz = pout * costh

    p1 = tf.stack([eout, px, py, pz], axis=1)
    p2 = tf.stack([eout, -px, -py, -pz], axis=1)

    # And the incoming momenta
    pa = tf.stack([ein, zeros, zeros, ein], axis=1)
    pb = tf.stack([ein, zeros, zeros, -ein], axis=1)
    out_p = tf.stack([pa, pb, p1, p2], axis=1)

    # Boost the momenta back from the COM of pa + pb
    eta = -0.5 * tf.math.log(x1 / x2)
    cth = tf.math.cosh(eta)
    sth = tf.math.sinh(eta)
    # Generate the boost matrix
    bE = tf.stack([cth, zeros, zeros, -sth], axis=1)
    bX = tf.stack([zeros, ones, zeros, zeros], axis=1)
    bY = tf.stack([zeros, zeros, ones, zeros], axis=1)
    bZ = tf.stack([-sth, zeros, zeros, cth], axis=1)

    bmat = tf.stack([bE, bX, bY, bZ], axis=1)
    # Apply boost
    total_p = tf.keras.backend.batch_dot(out_p, bmat, axes=2)

    # Include in the weight 1) flux factor 2) GeV to fb
    wgt *= float_me(389379365.6)  # pb
    wgt /= 2 * shat

    return total_p, wgt, x1, x2


###############################################################################


def luminosity(x1, x2, q2array):
    """ Returns f(x1)*f(x2) """
    # Note that the int_me are not needed if this function
    # were to be explicitly compiled (as would be in general)
    gluon_1 = PDF.xfxQ2(int_me([0]), x1, q2array)
    gluon_2 = PDF.xfxQ2(int_me([0]), x2, q2array)
    lumi = gluon_1 * gluon_2
    return lumi / x1 / x2

histo_bins = 10
fixed_bins = float_me([i*20 for i in range(histo_bins)])


# Integrand with accumulator:
def generate_integrand(cummulator_tensor, parser=None):
    """
    This function will generate an integrand function which will already hold a
    reference to the tensor to accumulate.

    Parameters
    ----------
        cummulator_tensor: tf.Variable
        parser: LheWriter, keeps track and stores vegasflow events. If None,
                use a dummy parser

    Returns
    -------
        cross_section_flow: function, vegasflow integrand
    """
    @tf.function
    def histogram_collector(results, variables):
        """ This function will receive a tensor (result)
        and the variables corresponding to those integrand results
        In the example integrand below, these corresponds to
            `final_result` and `histogram_values` respectively.
        `current_histograms` instead is the current value of the histogram
        which will be overwritten """
        # Fill a histogram with (10) PT bins with fixed distance
        indices = tf.histogram_fixed_width_bins(variables, [fixed_bins[0],fixed_bins[-1]] , nbins=histo_bins)
        t_indices = tf.transpose(indices)
        # Then consume the results with the utility we provide
        partial_hist = consume_array_into_indices(results, t_indices, histo_bins)
        # Then update the results of current_histograms
        new_histograms = partial_hist + current_histograms
        cummulator_tensor.assign(new_histograms)


# Minimal working example of tf vectorized cross section function
    def cross_section_flow(xrand, weight=1.0, **kwargs):
        """ We need the weight to fill the historgams """
        res = 0.0
        for matrixflow in all_matrices_flow:
            all_ps, wts, x1, x2 = phasespace_generator(xrand, matrixflow.nexternal)
            pdf = luminosity(x1, x2, tf.ones_like(x1) * Q2)
            smatrices = matrixflow.smatrix(all_ps, *model_params) * wts
            res += smatrices * pdf
            # Histogram results on the pt of particle 3 (one of the tops)
            pt = tf.sqrt(all_ps[:,3,1]**2 + all_ps[:,3,2]**2)
            histogram_collector(res*weight, (pt,))
            if parser is not None:
                tf.py_function(func=parser.lhe_parser, inp=[all_ps, res*weight], Tout=DTYPE)
        return res

    return cross_section_flow


if __name__ == "__main__":
    arger = argparse.ArgumentParser(
        """
    Example script to integrate Madgraph tensorflow compatible generated matrix element.

    In order to generate comparable results it is necessary to set the seed (-s) and not compile the integrand
        ~$ ./integrate_example.py -s 4 -r
    results are expected to be equal.

    It is also possible to run both at the same time and get equal results by setting eager mode
    so that both runs are truly independent.
        ~$ ./integrate_example.py -s 4 -e

    """
    )
    arger.add_argument(
        "-n", "--nevents", help="Number of events to be run", type=int, default=int(1e4)
    )
    arger.add_argument(
        "-s", "--set_seed", help="Set the seed of the calculation", type=int, default=0
    )
    arger.add_argument(
        "-i", "--iterations", help="Number of iterations to be run", type=int, default=4
    )
    arger.add_argument(
        "-g", "--grid", help="Number of iterations for grid warmup", type=int, default=4
    )
    arger.add_argument(
        "-r", "--reproducible", help="Run in reproducible mode", action="store_true"
    )
    arger.add_argument(
        "-e", "--eager", help="Run eager", action="store_true"
    )
    arger.add_argument(
        "-p", "--path", help="Path with the madflow pyOut exported matrix element", type=Path
    )
    arger.add_argument(
        "--run", help="Run folder name", type=str, default="run_01"
    )
    arger.add_argument(
        "--no_unweight", help="Do no unweight events", action="store_true", default=False
    )
    arger.add_argument(
        "--event_target", help="Number of unweighted events", type=int, default=0
    )
    args = arger.parse_args()

    if args.eager:
        run_eager(True)

    if args.path:
        if not args.path.exists():
            raise ValueError(f"Cannot find {args.path}")
        folder = args.path
    else:
        folder = Path("../../mg5amcnlo/vegasflow_example")
    ### go to the madgraph folder and load up anything that you need
    original_path = copy.copy(sys.path)
    sys.path.insert(0, folder.as_posix())
    for matrix_file in glob.glob(f"{folder.as_posix()}/matrix_*.py"):
        matrix_name = re_name.findall(matrix_file)[-1]
        class_name = matrix_name.capitalize()
        # this seems unnecesarily complicated to load a class from a file by anyway
        module_spec = importlib.util.spec_from_file_location(matrix_name, matrix_file)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        # now with access to the module, fill the list of matrices (with the object instantiated)
        all_matrices_flow.append(getattr(module, class_name)())

    # use the last module to load its model (all matrices should be using the same one!)
    root_path = getattr(module, "root_path")
    import_ufo = getattr(module, "import_ufo")
    model = import_ufo.import_model(f"{root_path}/{base_model}")

    get_model_param = getattr(module, "get_model_param")
    model_params = get_model_param(model)

    # We have matrix and model parameters, clean the path
    sys.path = original_path
    ################################################

    nparticles = 4
    n_dim = (nparticles - 2) * 3 - 2
    n_warmup = args.grid
    n_iter = args.iterations
    n_events = args.nevents

    seed = args.set_seed

    # Run the Parallel ME
    new_vegas = VegasFlow(n_dim, n_events)
    new_vegas.set_seed(seed)
    ##  Create a reference to the histograms
    current_histograms = tf.Variable(float_me(tf.zeros(histo_bins)))

    # Warmup vegasflow grids
    integrand = generate_integrand(current_histograms)
    new_vegas.compile(integrand, compilable=not args.reproducible)
    # When running the histogram, pass the reference to the histogram so it is accumulated
    new_vegas.run_integration(n_warmup, histograms=(current_histograms,))

    # Now run vegasflow tracking generated events with lhe parser
    with LheWriter(folder, args.run, args.no_unweight, args.event_target) as lhe_writer:
        new_vegas.freeze_grid()
        integrand = generate_integrand(current_histograms, parser=lhe_writer)
        new_vegas.compile(integrand, compilable=not args.reproducible)
        result = new_vegas.run_integration(n_iter, histograms=(current_histograms,))
        lhe_writer.store_result(result)
    print(f"Histogram:")
    print(f"   pt_l   |   pt_u   |  ds/dpt")
    print(f"------------------------------")
    for i, wgt in enumerate(current_histograms.numpy()):
        pt_l = str(fixed_bins[i].numpy())
        if i < (histo_bins-1):
            pt_u = str(fixed_bins[i+1].numpy())
        else:
            pt_u = "inf"
        print(f"{pt_l.center(10)}|{pt_u.center(10)}| {wgt:.5f}")
