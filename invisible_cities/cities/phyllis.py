"""
-----------------------------------------------------------------------
                                Phyllis
-----------------------------------------------------------------------

From ancient Greek, Φυλλίς: it was the name of a Tracian princess,
converted into a nut tree.

This city produces the light and dark spectrum of PMTs for dedicated
calibration runs. This is achieved by selecting regions in the PMT
waveforms where LED pulses are expected and regions which end 2
microseconds before, respectively and integrating the content within
each region. The regions before LED pulses should only contain
electronics noise and dark counts giving the zero external light
approximation whereas those in time with the pulses will contain one or
more detected photoelectrons. The waveform integrals are split into two
groups: those with expected photoelectrons (light) and those without
expected photoelectrons (dark). Each group produces a different
spectrum.

The spectra can be produced in three flavours:
    - Without using any deconvolution algorithm and using the mode to
      subtract the baseline.
    - Using the standard deconvolution algorithm to remove the effect
      of the electronics and to subtract the baseline.
    - Using the deconvolution algorithm with a mau to remove the effect
      of the electronics and to subtract the baseline.
The tasks performed are:
    - Subtract the baseline   (only in the first case).
    - Deconvolve the waveform ( not in the first case).
    - Slice the waveforms.
    - Split into light and dark.
    - Integrate slices.
    - Histrogram the result.
"""
from operator  import add
from functools import partial

import numpy  as np
import tables as tb

from .. database               import                 load_db
from .. reco                   import           tbl_functions as tbl
from .. reco                   import         calib_functions as cf
from .. reco                   import calib_sensors_functions as csf
from .. sierpe                 import                     fee
from .. io   .    histogram_io import             hist_writer
from .. io   .run_and_event_io import    run_and_event_writer
from .. core .core_functions   import    shift_to_bin_centers

from .. dataflow import dataflow as fl

from .  components import city
from .  components import WfType
from .  components import print_every
from .  components import sensor_data
from .  components import wf_from_files
from .  components import waveform_binner
from .  components import deconv_pmt
from .  components import waveform_integrator


@city
def phyllis(files_in, file_out, compression, event_range, print_mod, detector_db, run_number,
            proc_mode, n_baseline,
            min_bin, max_bin, bin_width,
            number_integrals, integral_start, integral_width, integrals_period,
            n_mau = 100):
    if   proc_mode == "gain"         : proc = pmt_deconvolver    (detector_db, run_number, n_baseline       )
    elif proc_mode == "gain_mau"     : proc = pmt_deconvolver_mau(detector_db, run_number, n_baseline, n_mau)
    elif proc_mode == "gain_nodeconv": proc = mode_subtractor    (detector_db, run_number)
    else                             : raise ValueError(f"Unrecognized processing mode: {proc_mode}")

    bin_edges   = np.arange(min_bin, max_bin, bin_width)
    bin_centres = shift_to_bin_centers(bin_edges)
    sd          = sensor_data(files_in[0], WfType.rwf)
    npmt        = np.count_nonzero(load_db.DataPMT(detector_db, run_number).Active.values)
    wf_length   = sd.PMTWL
    shape       = npmt, len(bin_centres)
    sampling    = fee.t_sample

    (light_limits,
      dark_limits) = cf.valid_integral_limits(sampling        ,
                                              number_integrals,
                                              integral_start  ,
                                              integral_width  ,
                                              integrals_period,
                                              wf_length       )

    processing        = fl.map(proc, args="pmt", out="cwf")
    integrate_light   = fl.map(waveform_integrator(light_limits))
    integrate_dark    = fl.map(waveform_integrator( dark_limits))
    bin_waveforms     = fl.map(waveform_binner    (  bin_edges ))
    sum_histograms    = fl.reduce(add, np.zeros(shape, dtype=int))
    accumulate_light  = sum_histograms()
    accumulate_dark   = sum_histograms()
    event_count       = fl.spy_count()

    with tb.open_file(file_out, 'w', filters=tbl.filters(compression)) as h5out:
        write_event_info    = run_and_event_writer(h5out)
        write_run_and_event = fl.sink(write_event_info, args=("run_number", "event_number", "timestamp"))
        write_hist          = partial(hist_writer,
                                      h5out,
                                      group_name  = 'HIST',
                                      n_sensors   = npmt,
                                      bin_centres = bin_centres)

        out = fl.push(
            source = wf_from_files(files_in, WfType.rwf),
            pipe   = fl.pipe(fl.slice(*event_range, close_all=True),
                             event_count.spy,
                             print_every(print_mod),
                             processing,
                             fl.fork(("cwf", integrate_light, bin_waveforms, accumulate_light   .sink),
                                     ("cwf", integrate_dark , bin_waveforms, accumulate_dark    .sink),
                                                                             write_run_and_event      )),

            result = dict(events_in   = event_count     .future,
                          spe         = accumulate_light.future,
                          dark        = accumulate_dark .future,
                          event_count =      event_count.future)
        )

        write_hist(table_name = 'pmt_spe' )(out.spe )
        write_hist(table_name = 'pmt_dark')(out.dark)
        cf.copy_sensor_table(files_in[0], h5out)

    return out


def pmt_deconvolver(detector_db, run_number, n_baseline):
    deconvolute = deconv_pmt(detector_db, run_number, n_baseline)
    return deconvolute


def pmt_deconvolver_mau(detector_db, run_number, n_baseline, n_mau):
    deconvolute = pmt_deconvolver(detector_db, run_number, n_baseline)
    def deconv_pmt_mau(rwf):
        cwf = deconvolute(rwf)
        return csf.pmt_subtract_mau(cwf, n_mau)
    return deconv_pmt_mau


def mode_subtractor(detector_db, run_number):
    active = load_db.DataPMT(detector_db, run_number).Active.values
    active = np.nonzero(active)[0].tolist()
    def subtract_mode(rwf):
        return csf.subtract_mode(rwf)[active]
    return subtract_mode
