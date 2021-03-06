"""bcbio qc module. Parsers for collecting qc metrics."""
import os
import sys
import re
import yaml
import xml.parsers.expat
import hashlib
import time
from uuid import uuid4
import glob
import json
import fnmatch
import numpy as np
import csv
from collections import defaultdict
from itertools import izip

from bs4 import BeautifulSoup

from cement.core import backend
LOG = backend.minimal_logger("bcbio")

from bcbio.broad.metrics import *
from bcbio.pipeline.qcsummary import FastQCParser

class MetricsParser():
    """Basic class for parsing metrics"""
    def __init__(self, log=None):
        self.log = LOG
        if log:
            self.log = log

    def parse_bc_metrics(self, in_handle):
        data = {}
        while 1:
            line = in_handle.readline()
            if not line:
                break
            vals = line.rstrip("\t\n\r").split("\t")
            data[vals[0]] = int(vals[1])
        return data
    
    def parse_filter_metrics(self, in_handle):
        data = {}
        data["reads"] = int(in_handle.readline().rstrip("\n").split(" ")[-1])
        data["reads_aligned"] = int(in_handle.readline().split(" ")[-2])
        data["reads_fail_align"] = int(in_handle.readline().split(" ")[-2])
        return data

    def parse_fastq_screen_metrics(self, in_handle):
        column_names = ["Library", "Unmapped", "Mapped_One_Library", "Mapped_Multiple_Libraries"]
        in_handle.readline()
        data = {}
        while 1:
            line = in_handle.readline()
            if not line:
                break
            vals = line.rstrip("\t\n").split("\t")
            data[vals[0]] = {}
            data[vals[0]]["Unmapped"] = float(vals[1])
            data[vals[0]]["Mapped_One_Library"] = float(vals[2])
            data[vals[0]]["Mapped_Multiple_Libraries"] = float(vals[3])
        return data


class ExtendedPicardMetricsParser(PicardMetricsParser):
    """Extend basic functionality and parse all picard metrics"""

    def __init__(self):
        PicardMetricsParser.__init__(self)

    def _get_command(self, in_handle):
        analysis = None
        while 1:
            line = in_handle.readline()
            if line.startswith("# net.sf.picard.analysis") or line.startswith("# net.sf.picard.sam"):
                break
        return line.rstrip("\n")

    def _read_off_header(self, in_handle):
        while 1:
            line = in_handle.readline()
            if line.startswith("## METRICS"):
                break
        return in_handle.readline().rstrip("\n").split("\t")

    def _read_vals_of_interest(self, want, header, info):
        want_indexes = [header.index(w) for w in header]
        vals = dict()
        for i in want_indexes:
            vals[header[i]] = info[i]
        return vals

    def _parse_align_metrics(self, in_handle):
        command = self._get_command(in_handle)
        header = self._read_off_header(in_handle)
        d = dict([[x, []] for x in header])
        res = dict(command=command, FIRST_OF_PAIR = d, SECOND_OF_PAIR = d, PAIR = d)
        while 1:
            info = in_handle.readline().rstrip("\n").split("\t")
            category = info[0]
            if len(info) <= 1:
                break
            vals = self._read_vals_of_interest(header, header, info)
            res[category] = vals
        return res

    def _parse_dup_metrics(self, in_handle):
        command = self._get_command(in_handle)
        header = self._read_off_header(in_handle)
        info = in_handle.readline().rstrip("\n").split("\t")
        vals = self._read_vals_of_interest(header, header, info)
        histvals = self._read_histogram(in_handle)
        return dict(command=command, metrics = vals, hist = histvals)

    def _parse_insert_metrics(self, in_handle):
        command = self._get_command(in_handle)
        header = self._read_off_header(in_handle)
        info = in_handle.readline().rstrip("\n").split("\t")
        vals = self._read_vals_of_interest(header, header, info)
        histvals = self._read_histogram(in_handle)
        return dict(command=command, metrics = vals, hist = histvals)

    def _parse_hybrid_metrics(self, in_handle):
        command = self._get_command(in_handle)
        header = self._read_off_header(in_handle)
        info = in_handle.readline().rstrip("\n").split("\t")
        vals = self._read_vals_of_interest(header, header, info)
        return dict(command=command, metrics = vals)
    
    def _read_histogram(self, in_handle):
        labels = self._read_to_histogram(in_handle)
        if labels is None:
            return None
        vals = dict([[x, []] for x in labels])
        while 1:
            line = in_handle.readline()
            info = line.rstrip("\n").split("\t")
            if len(info) < len(labels):
                break
            for i in range(0, len(labels)):
                vals[labels[i]].append(info[i])
        return vals

    def _read_to_histogram(self, in_handle):
        while 1:
            line = in_handle.readline()
            if line.startswith("## HISTOGRAM"):
                break
            if not line:
                return None
        return in_handle.readline().rstrip("\n").split("\t")
          
class RunInfoParser():
    """RunInfo parser"""
    def __init__(self):
        self._data = {}
        self._element = None

    def parse(self, fp):
        if os.path.exists(fp):
            self._parse_RunInfo(fp)
        return self._data

    def _start_element(self, name, attrs):
        self._element=name
        if name == "Run":
            self._data["Id"] = attrs["Id"]
            self._data["Number"] = attrs["Number"]
        elif name == "FlowcellLayout":
            self._data["FlowcellLayout"] = attrs
        elif name == "Read":
            self._data["Reads"].append(attrs)
            
    def _end_element(self, name):
        self._element=None

    def _char_data(self, data):
        want_elements = ["Flowcell", "Instrument", "Date"]
        if self._element in want_elements:
            self._data[self._element] = data
        if self._element == "Reads":
            self._data["Reads"] = []

    def _parse_RunInfo(self, fp):
        p = xml.parsers.expat.ParserCreate()
        p.StartElementHandler = self._start_element
        p.EndElementHandler = self._end_element
        p.CharacterDataHandler = self._char_data
        p.ParseFile(fp)


class IlluminaXMLParser():
    """Illumina xml data parser. Parses xml files in flowcell directory."""
    def __init__(self):
        self._data = {}
        self._element = None
        self._tmp = None
        self._header = None

    def _chart_start_element(self, name, attrs):
        self._element = name
        if name == "FlowCellData":
            self._header = attrs
        if name == "Layout":
            n_tiles_per_lane = int(attrs['RowsPerLane']) * int(attrs['ColsPerLane'])
            nrow = int(attrs['NumLanes']) * n_tiles_per_lane
            if self._tmp is None:
                self._tmp = self._header
                self._tmp.update(attrs)
                for i in range(1,int(attrs['NumLanes'])+1):
                    for j in range(1,n_tiles_per_lane+1):
                        key = "%s_%s" % (i,j)
                        self._tmp[key] = {}
                     
        if name == "TL":
            self._tmp[attrs["Key"]][self._index] = {}
            for k in attrs.keys():
                if k == "Key":
                    continue
                if attrs[k] == "NaN":
                    v = None
                else:
                    v = float(attrs[k])
                                        
                self._tmp[attrs["Key"]][self._index] = v

    def _chart_end_element(self, name):
        self._element = None
    def _chart_char_data(self, data):
        pass

    def _parse_charts(self, files):
        for f in files:
            self._index = os.path.basename(f).rstrip(".xml").lstrip("Chart_")
            p = xml.parsers.expat.ParserCreate()
            p.StartElementHandler = self._chart_start_element
            p.EndElementHandler = self._chart_end_element
            p.CharacterDataHandler = self._chart_char_data
            fp = open(f)
            p.ParseFile(fp)
            fp.close()

    def _summary_start_element(self, name, attrs):
        self._element = name
        if name == "Summary":
            self._tmp[self._index] = attrs
        if name == "Lane":
            self._tmp[self._index][attrs['key']] = attrs
    def _summary_end_element(self, name):
        self._element = None
    def _summary_char_data(self, data):
        pass

    def _parse_summary(self, files):
        for f in files:
            self._index = os.path.basename(f).rstrip(".xml")
            p = xml.parsers.expat.ParserCreate()
            p.StartElementHandler = self._summary_start_element
            p.EndElementHandler = self._summary_end_element
            p.CharacterDataHandler = self._summary_char_data
            fp = open(f)
            p.ParseFile(fp)
            fp.close()

    def _clusters_start_element(self, name, attrs):
        self._element = name
        if name == "Data":
            self._tmp[self._index] = attrs
        if name == "Lane":
            self._tmp[self._index][attrs['key']] = attrs
    def _clusters_end_element(self, name):
        self._element = None
    def _clusters_char_data(self, data):
        pass

    def _parse_clusters(self, files):
        for f in files:
            self._index = os.path.basename(f).rstrip(".xml")
            p = xml.parsers.expat.ParserCreate()
            p.StartElementHandler = self._clusters_start_element
            p.EndElementHandler = self._clusters_end_element
            p.CharacterDataHandler = self._clusters_char_data
            fp = open(f)
            p.ParseFile(fp)
            fp.close()

    ## Caution: no assert statements for file existence
    def parse(self, files, fullRTA=False):
        """Full parsing includes all RTA files"""
        if fullRTA:
            error_files = filter(lambda x: os.path.dirname(x).endswith("ErrorRate"), files)
            self._parse_charts(error_files)
            self._data["ErrorRate"] = self._tmp
            self._tmp = None
            FWHM_files = filter(lambda x: os.path.dirname(x).endswith("FWHM"), files)
            self._parse_charts(FWHM_files)
            self._data["FWHM"] = self._tmp
            self._tmp = None
            intensity_files = filter(lambda x: os.path.dirname(x).endswith("Intensity"), files)
            self._parse_charts(intensity_files)
            self._data["Intensity"] = self._tmp
            self._tmp = None
            numgt30_files = filter(lambda x: os.path.dirname(x).endswith("NumGT30"), files)
            self._parse_charts(numgt30_files)
            self._data["NumGT30"] = self._tmp
            self._tmp = None
            chart_files = filter(lambda x: os.path.basename(x).endswith("_Chart.xml"), files)
            self._parse_charts(chart_files)
            self._data["Charts"] = self._tmp
            self._tmp = None

        ## Parse Summary and clusters
        self._tmp = {}
        summary_files = filter(lambda x: os.path.dirname(x).endswith("Summary"), files)
        self._parse_summary(summary_files)
        self._data["Summary"] = self._tmp
        self._tmp = {}
        cluster_files = filter(lambda x: os.path.basename(x).startswith("NumClusters By"), files)
        self._parse_clusters(cluster_files)
        self._data["NumClusters"] = self._tmp

        return self._data

class ExtendedFastQCParser(FastQCParser):
    def __init__(self, base_dir):
        FastQCParser.__init__(self, base_dir)

    def get_fastqc_summary(self):
        metric_labels = ["Per base sequence quality", "Basic Statistics", "Per sequence quality scores",
                         "Per base sequence content", "Per base GC content", "Per sequence GC content",
                         "Per base N content", "Sequence Length Distribution", "Sequence Duplication Levels",
                         "Overrepresented sequences", "Kmer Content"]
        metrics = {x : self._to_dict(self._fastqc_data_section(x)) for x in metric_labels}
        return metrics

    def _to_dict(self, section):
        if len(section) == 0:
            return {}
        header = [x.strip("#") for x in section[0].rstrip("\t").split("\t")]
        d = []
        for l in section[1:]:
            d.append(l.split("\t"))
        data = np.array(d)
        df = {header[i]:data[:,i].tolist() for i in range(0,len(header))}
        return df
##############################
##  objects
##############################
class RunMetrics(dict):
    """Generic Run class"""
    _metrics = []
    ## Following paths are ignored
    ignore = "|".join(["tmp", "tx", "-split", "log"])
    reignore = re.compile(ignore)

    def __init__(self, log=None):
        self["_id"] = uuid4().hex
        self["entity_type"] = self.entity_type()
        self["name"] = None
        self["creation_time"] = None
        self["modification_time"] = None
        self.files = []
        self.path=None
        self.log = LOG
        if log:
            self.log = log

    def entity_type(self):
        return type(self).__name__
    
    def get_db_id(self):
        return self["_id"]

    def to_json(self):
        return json.dumps(self)

    def parse(self):
        raise NotImplementedError

    def _collect_files(self):
        if not self.path:
            return
        if not os.path.exists(self.path):
            raise IOError
        self.files = []
        for root, dirs, files in os.walk(self.path):
            if re.search(self.reignore, root):
                continue
            self.files = self.files + [os.path.join(root, x) for x in files]

    def filter_files(self, pattern, filter_fn=None):
        """Take file list and return those files that pass the filter_fn criterium"""
        def filter_function(f):
            return re.search(pattern, f) != None
        if not filter_fn:
            filter_fn = filter_function
        return [x for x in filter(filter_fn, self.files)]

class SampleRunMetrics(RunMetrics):
    """Sample-level class for holding run metrics data"""

    def __init__(self, path, flowcell, date, lane, barcode_name, barcode_id, sample_prj, sequence="NoIndex", barcode_type=None, genomes_filter_out=None):
        RunMetrics.__init__(self)
        self.path = path
        self["entity_type"] = "sample_run_metrics"
        self["barcode_id"] = barcode_id
        self["barcode_name"] = barcode_name
        self["barcode_type"] = barcode_type
        self["bc_count"] = None
        self["date"] = date
        self["flowcell"] = flowcell
        self["lane"] = lane
        self["sample_prj"] = sample_prj
        self["sequence"] = sequence
        self["barcode_type"] = barcode_type
        self["genomes_filter_out"] = genomes_filter_out
        self["name"] = "{}_{}_{}_{}".format(lane, date, flowcell, sequence)
        
        ## Metrics
        self["fastqc"] = {}
        self["fastq_scr"] = {}
        self["picard_metrics"] = {}

        self._collect_files()

    def __repr__(self):
        return "<sample_run_metrics {}>".format(self["name"])

    def __str__(self):
        return repr(self)
        
    def read_picard_metrics(self):
        self.log.debug("read_picard_metrics for sample {}, project {}, lane {} in run {}".format(self["barcode_name"], self["sample_prj"], self["lane"], self["flowcell"]))
        picard_parser = ExtendedPicardMetricsParser()
        pattern = "|".join(["{}_[0-9]+_[0-9A-Za-z]+(_nophix)?_{}-.*.(align|hs|insert|dup)_metrics".format(self["lane"], self["barcode_id"]),
                            "{}_[0-9]+_[0-9A-Za-z]+_{}(_nophix)?-.*.(align|hs|insert|dup)_metrics".format(self["lane"], self["barcode_id"])])
        files = self.filter_files(pattern)
        if len(files) == 0:
            self.log.warn("no picard metrics files for sample {}; pattern {}".format(self["barcode_name"], pattern))
            return 
        try:
            self.log.debug("files {}".format(",".join(files)))
            metrics = picard_parser.extract_metrics(files)
            self["picard_metrics"] = metrics
        except:
            self.log.warn("no picard metrics for sample {}".format(self["barcode_name"]))

    def parse_fastq_screen(self):
        self.log.debug("parse_fastq_screen for sample {}, project {}, lane {} in run {}".format(self["barcode_name"], self["sample_prj"], self["lane"], self["flowcell"]))
        parser = MetricsParser()
        pattern = "|".join(["{}_[0-9]+_[0-9A-Za-z]+(_nophix)?_{}_[12]_fastq_screen.txt".format(self["lane"], self["barcode_id"]),
                            "{}_[0-9]+_[0-9A-Za-z]+_{}(_nophix)?_[12]_fastq_screen.txt".format(self["lane"], self["barcode_id"])])
        files = self.filter_files(pattern)
        self.log.debug("files {}".format(",".join(files)))
        try:
            fp = open(files[0])
            data = parser.parse_fastq_screen_metrics(fp)
            fp.close()
            self["fastq_scr"] = data
        except:
            self.log.warn("no fastq screen metrics for sample {}".format(self["barcode_name"]))

    def read_fastqc_metrics(self):
        self.log.debug("read_fastq_metrics for sample {}, project {}, lane {} in run {}".format(self["barcode_name"], self["sample_prj"], self["lane"], self["flowcell"]))
        if self["barcode_name"] == "unmatched":
            return
        self["fastqc"] = {'stats':None}
        pattern = "fastqc/{}_[0-9]+_[0-9A-Za-z]+(_nophix)?_{}-*".format(self["lane"], self["barcode_id"])
        files = self.filter_files(pattern)
        self.log.debug("files {}".format(",".join(files)))
        try:
            fastqc_dir = os.path.dirname(files[0])
            fqparser = ExtendedFastQCParser(fastqc_dir)
            stats = fqparser.get_fastqc_summary()
            self["fastqc"] = {'stats':stats}
        except:
            self.log.warn("no fastq screen metrics for sample {}".format(self["barcode_name"]))

    def parse_filter_metrics(self):
        """CASAVA: Parse filter metrics at sample level"""
        self.log.debug("parse_filter_metrics for lane {}, project {} in flowcell {}".format(self["lane"], self["sample_prj"], self["flowcell"]))
        pattern = "{}_[0-9]+_[0-9A-Za-z]+_{}(_nophix)?.filter_metrics".format(self["lane"], self["barcode_id"])
        files = self.filter_files(pattern)
        self.log.debug("files {}".format(",".join(files)))
        self["filter_metrics"] = {"reads":None, "reads_aligned":None, "reads_fail_align":None}
        try:
            fp = open(files[0])
            parser = MetricsParser()
            data = parser.parse_filter_metrics(fp)
            fp.close()
            self["filter_metrics"] = data
        except:
            self.log.warn("No filter nophix metrics for lane {}".format(self["lane"]))

    def parse_bc_metrics(self):
        """Parse bc metrics at sample level"""
        self.log.debug("parse_bc_metrics for sample {}, project {} in flowcell {}".format(self["barcode_name"], self["sample_prj"], self["flowcell"]))
        pattern = "{}_[0-9]+_[0-9A-Za-z]+(_nophix)?[\._]bc[\._]metrics".format(self["lane"])
        files = self.filter_files(pattern)
        if len(files) == 0:
            self.log.debug("no bc metrics files for sample {}; pattern {}".format(self["barcode_name"], pattern))
            return 
        self.log.debug("files {}".format(",".join(files)))
        try:
            parser = MetricsParser()
            fp = open(files[0])
            data = parser.parse_bc_metrics(fp)
            fp.close()
            self["bc_count"] = data[str(self["barcode_id"])]
        except:
            self.log.warn("No bc_metrics info for lane {}".format(self["lane"]))


class FlowcellRunMetrics(RunMetrics):
    """Flowcell level class for holding qc data."""
    def __init__(self, path, fc_date, fc_name, runinfo="RunInfo.xml"):#, parse=True, fullRTA=False):
        RunMetrics.__init__(self)
        self.path = path
        self.db=None
        self.fc_name = fc_name
        self["name"] = "{}_{}".format(fc_date, fc_name)
        self["RunInfo"] = {"Id" : self["name"], "Flowcell":fc_name, "Date": fc_date, "Instrument": "NA"}
        self["run_info_yaml"] = {}
        self["samplesheet_csv"] = {}
        self._lanes = [1,2,3,4,5,6,7,8]
        self["lanes"] = {str(k):{"lane":str(k), "filter_metrics":{}, "bc_metrics":{}} for k in self._lanes}
        self["illumina"] = {}
        self._parseRunInfo(runinfo)
        self._collect_files()

    def __repr__(self):
        return "<flowcell_metrics {}>".format(self["name"])

    def __str__(self):
        return repr(self)

    def _parseRunInfo(self, fn="RunInfo.xml"):
        infile = os.path.join(os.path.abspath(self.path), fn)
        self.log.debug("_parseRunInfo: going to read {}".format(infile))
        try:
            fp = open(infile)
            parser = RunInfoParser()
            data = parser.parse(fp)
            fp.close()
            self["RunInfo"] = data
        except:
            self.log.warn("No such file %s" % os.path.join(os.path.abspath(self.path), fn))

    def parse_samplesheet_csv(self):
        infile = os.path.join(os.path.abspath(self.path), "{}.csv".format(self["RunInfo"]["Flowcell"][1:]))
        self.log.debug("parse_samplesheet_csv: going to read {}".format(infile))
        try:
            fp = open(infile)
            runinfo = json.dumps([x for x in csv.reader(fp)])
            fp.close()
            self["samplesheet_csv"] = runinfo
            return True
        except:
            self.log.warn("No such file {}".format(infile))
            return False
            
    def parse_run_info_yaml(self, run_info_yaml="run_info.yaml"):
        infile = os.path.join(os.path.abspath(self.path), run_info_yaml)
        self.log.debug("parse_run_info_yaml: going to read {}".format(infile))
        try:
            fp = open(infile)
            runinfo = yaml.load(fp)
            fp.close()
            self["run_info_yaml"] = runinfo
            return True
        except:
            self.log.warn("No such file {}".format(infile))
            return False

    def get_full_flowcell(self):
        vals = self["RunInfo"]["Id"].split("_")
        return vals[-1]
    def get_flowcell(self):
        return self.get("metrics").get("RunInfo").get("Flowcell")
    def get_date(self):
        return self.get("metrics").get("RunInfo").get("Date")
    def get_run_name(self):
        return "%s_%s" % (self.get_date(), self.get_full_flowcell())

    def parse_illumina_metrics(self, fullRTA):
        self.log.debug("parse_illumina_metrics")
        fn = []
        for root, dirs, files in os.walk(os.path.abspath(self.path)):
            for file in files:
                if file.endswith(".xml"):
                    fn.append(os.path.join(root, file))
        self.log.debug("Found {} RTA files {}...".format(len(fn), ",".join(fn[0:10])))
        parser = IlluminaXMLParser()
        metrics = parser.parse(fn, fullRTA)
        self["illumina"].update(metrics)

    def parse_filter_metrics(self):
        """pre-CASAVA: Parse filter metrics at flowcell level"""
        self.log.debug("parse_filter_metrics for flowcell {}".format(self["RunInfo"]["Flowcell"]))
        for lane in self._lanes:
            pattern = "{}_[0-9]+_[0-9A-Za-z]+(_nophix)?.filter_metrics".format(lane)
            self["lanes"][str(lane)]["filter_metrics"] = {"reads":None, "reads_aligned":None, "reads_fail_align":None}
            files = self.filter_files(pattern)
            self.log.debug("filter metrics files {}".format(",".join(files)))
            try:
                fp = open(files[0])
                parser = MetricsParser()
                data = parser.parse_filter_metrics(fp)
                fp.close()
                self["lanes"][str(lane)]["filter_metrics"] = data
            except:
                self.log.warn("No filter nophix metrics for lane {}".format(lane))

    def parse_bc_metrics(self):
        """Parse bc metrics at sample level"""
        self.log.debug("parse_bc_metrics for flowcell {}".format(self["RunInfo"]["Flowcell"]))
        for lane in self._lanes:
            pattern = "{}_[0-9]+_[0-9A-Za-z]+(_nophix)?[\._]bc[\._]metrics".format(lane)
            self["lanes"][str(lane)]["bc_metrics"] = {"reads":None, "reads_aligned":None, "reads_fail_align":None}
            files = self.filter_files(pattern)
            self.log.debug("bc metrics files {}".format(",".join(files)))
            try:
                parser = MetricsParser()
                fp = open(files[0])
                data = parser.parse_bc_metrics(fp)
                fp.close()
                self["lanes"][str(lane)]["bc_metrics"] = data
            except:
                self.log.warn("No bc_metrics info for lane {}".format(lane))

    def parse_demultiplex_stats_htm(self):
        """Parse the Unaligned/Basecall_Stats_*/Demultiplex_Stats.htm file
        generated from CASAVA demultiplexing and returns barcode metrics.
        """
        htm_file = os.path.join(self.path, "Unaligned", "Basecall_Stats_{}".format(self.fc_name[1:]), "Demultiplex_Stats.htm")
        self.log.debug("parsing {}".format(htm_file))
        if not os.path.exists(htm_file):
            self.log.warn("No such file {}".format(htm_file))
            return
        with open(htm_file) as fh:
            htm_doc = fh.read()
        soup = BeautifulSoup(htm_doc)
        ## 
        ## Find headers
        allrows = soup.findAll("tr")
        column_gen=(row.findAll("th") for row in allrows)
        parse_row = lambda row: row 
        headers = [h for h in map(parse_row, column_gen) if h]
        bc_header = [str(x.string) for x in headers[0]]
        smp_header = [str(x.string) for x in headers[1]]
        ## 'Known' headers from a Demultiplex_Stats.htm document
        bc_header_known = ['Lane', 'Sample ID', 'Sample Ref', 'Index', 'Description', 'Control', 'Project', 'Yield (Mbases)', '% PF', '# Reads', '% of raw clusters per lane', '% Perfect Index Reads', '% One Mismatch Reads (Index)', '% of >= Q30 Bases (PF)', 'Mean Quality Score (PF)']
        smp_header_known = ['None', 'Recipe', 'Operator', 'Directory']
        if not bc_header == bc_header_known:
            self.log.warn("Barcode lane statistics header information has changed. New format?\nOld format: {}\nSaw: {}".format(",".join((["'{}'".format(x) for x in bc_header_known])), ",".join(["'{}'".format(x) for x in bc_header])))
        if not smp_header == smp_header_known:
            self.log.warn("Sample header information has changed. New format?\nOld format: {}\nSaw: {}".format(",".join((["'{}'".format(x) for x in smp_header_known])), ",".join(["'{}'".format(x) for x in smp_header])))
        ## Fix first header name in smp_header since htm document is mal-formatted: <th>Sample<p></p>ID</th>
        smp_header[0] = "Sample ID"

        metrics = {}
        ## Parse Barcode lane statistics
        soup = BeautifulSoup(htm_doc)
        table = soup.findAll("table")[1]
        rows = table.findAll("tr")
        column_gen = (row.findAll("td") for row in rows)
        parse_row = lambda row: {bc_header[i]:str(row[i].string) for i in range(0, len(bc_header)) if row}
        metrics["Barcode_lane_statistics"] =  map(parse_row, column_gen)

        ## Parse Sample information
        soup = BeautifulSoup(htm_doc)
        table = soup.findAll("table")[3]
        rows = table.findAll("tr")
        column_gen = (row.findAll("td") for row in rows)
        parse_row = lambda row: {smp_header[i]:str(row[i].string) for i in range(0, len(smp_header)) if row}
        metrics["Sample_information"] = map(parse_row, column_gen)
        ## Set data
        self["illumina"].update({"Demultiplex_Stats": metrics})
        return metrics
