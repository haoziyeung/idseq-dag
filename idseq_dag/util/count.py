import multiprocessing

from enum import Enum

import idseq_dag.util.command as command
import idseq_dag.util.command_patterns as command_patterns
import idseq_dag.util.fasta as fasta

from idseq_dag import __version__


class ReadCountingMode(Enum):
    COUNT_UNIQUE = "COUNT UNIQUE READS"
    COUNT_ALL = "COUNT ALL READS"


# Feel free to delete the support for COUNT_UNIQUE after pipeline 4.0 has been released and well accepted.
PIPELINE_MAJOR_VERSION = int(__version__.split(".", 1)[0])
READ_COUNTING_MODE = ReadCountingMode.COUNT_ALL if PIPELINE_MAJOR_VERSION >= 4 else ReadCountingMode.COUNT_UNIQUE


def _count_reads_via_wc(local_file_path, max_reads):
    '''
    Count reads in a local file based on file format inferred from extension,
    up to a maximum of max_reads.
    '''
    if local_file_path.endswith(".gz"):
        cmd = r'''zcat "${local_file_path}"'''
        file_format = local_file_path.split(".")[-2]
    else:
        cmd = r'''cat "${local_file_path}"'''
        file_format = local_file_path.split(".")[-1]

    named_args = {
        'local_file_path': local_file_path
    }

    if max_reads:
        max_lines = _reads2lines(max_reads, file_format)
        assert max_lines is not None, "Could not convert max_reads to max_lines"
        cmd += r''' | head -n "${max_lines}"'''
        named_args.update({
            'max_lines': max_lines
        })

    cmd += " |  wc -l"

    cmd_output = command.execute_with_output(
        command_patterns.ShellScriptCommand(
            script=cmd,
            named_args=named_args
        )
    )
    line_count = int(cmd_output.strip().split(' ')[0])
    return _lines2reads(line_count, file_format)


def _lines2reads(line_count, file_format):
    '''
    Convert line count to read count based on file format.
    Supports fastq and SINGLE-LINE fasta formats.
    TODO: add support for m8 files here once the relevant steps are added in the pipeline engine.
    '''
    read_count = None
    if file_format in ["fq", "fastq"]:
        # Each read consists of exactly 4 lines, by definition.
        assert line_count % 4 == 0, "File does not follow fastq format"
        read_count = line_count // 4
    elif file_format in ["fa", "fasta"]:
        # Each read consists of exactly 2 lines (identifier and sequence).
        # ASSUMES the format is SINGLE-LINE fasta files, where each read's sequence
        # is on a single line rather than being spread over multiple lines.
        # This is fine for now, because we only count fasta files we generated
        # ourselves, following the single-line format.
        assert line_count % 2 == 0, "File does not follow single-line fasta format"
        read_count = line_count // 2
    else:
        read_count = line_count
    return read_count


def _reads2lines(read_count, file_format):
    '''
    Convert read count to line count based on file format.
    Currently supports fastq or SINGLE-LINE fasta.
    '''
    line_count = None
    if file_format in ["fq", "fastq"]:
        line_count = 4 * read_count
    elif file_format in ["fa", "fasta"]:
        # Assumes the format is single-line fasta rather than multiline fasta
        line_count = 2 * read_count
    return line_count


def files_have_min_reads(input_files, min_reads):
    """ Checks whether fa/fasta/fq/fastq have the minimum number of reads.
    Pipeline steps can use this method for input validation.
    """
    for input_file in input_files:
        num_reads = _count_reads_via_wc(input_file, max_reads=min_reads)
        if num_reads < min_reads:
            return False
    return True


def _count_reads_expanding_duplicates(local_file_path, cluster_sizes, cluster_key):
    # See documentation for reads_in_group use case with cluster_sizes, below.
    unique_count, nonunique_count = 0, 0
    for read in fasta.iterator(local_file_path):
        # A read header looks someting like
        #
        #    >M05295:357:000000000-CRPNR:1:1101:22051:10534 OPTIONAL RANDOM STUFF"
        #     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
        #
        # where the first character on the line is '>' and the read ID (underlined above)
        # extends from '>' to the first whitespace character, not including '>' itself.
        #
        # The fasta iterator already asserts that read.header[0] is '>'.
        #
        # As we proceed down along the pipeline, read IDs get annotated with taxonomic information,
        # changing the above into something like
        #
        #   >NT:ABC2433.1:NR:ABC5656.2:M05295:357:000000000-CRPNR:1:1101:22051:10534 OPTIONAL RANDOM STUFF"
        #    ^^^^^^^^^^^^^^^^^^^^^^^^^^
        #
        # The underlined annotation has to be stripped out by the cluster_key function,
        # so that we can use the original read ID to look up the cluster size.
        #
        read_id = read.header.split(None, 1)[0][1:]
        unique_count += 1
        nonunique_count += get_read_cluster_size(cluster_sizes, cluster_key(read_id))
    return unique_count, nonunique_count


def get_read_cluster_size(cdhit_cluster_sizes, read_id):
    suffix = None
    cluster_size = cdhit_cluster_sizes.get(read_id)
    if cluster_size == None:
        prefix, suffix = read_id[:-2], read_id[-2:]
        cluster_size = cdhit_cluster_sizes.get(prefix)
    assert cluster_size != None and suffix in (
        None, "/1", "/2"), f"Read ID not found in cdhit_cluster_sizes dict: {read_id}"
    return cluster_size


def _load_cdhit_clusters_work(filename, headers=False):
    cdhit_cluster_sizes = {}
    with open(filename, "r") as f:
        for line in f:
            # TODO: (gdingle): test me
            if headers:
                cluster_size_str, *read_ids = line.split(None)
                cdhit_cluster_sizes[read_id.strip()] = read_ids
            else:
                cluster_size_str, read_id = line.split(None)[0:2]
                cdhit_cluster_sizes[read_id.strip()] = int(cluster_size_str)
    return cdhit_cluster_sizes


# Loading cluster sizes can be expensive prior to subsampling (for some exceptionally large
# samples with over 100 million reads).  To ameliorate this cost, we make sure it is only
# paid once per stage (not once per step).
_CDHIT_CLUSTER_SIZES_CACHE = {}
_CDHIT_CLUSTER_SIZES_LOCK = multiprocessing.RLock()


def load_cdhit_cluster_sizes(filename):
    with _CDHIT_CLUSTER_SIZES_LOCK:
        if filename not in _CDHIT_CLUSTER_SIZES_CACHE:
            _CDHIT_CLUSTER_SIZES_CACHE[filename] = _load_cdhit_clusters_work(filename)
        return _CDHIT_CLUSTER_SIZES_CACHE[filename]


def load_cdhit_cluster_headers(filename):
    with _CDHIT_CLUSTER_SIZES_LOCK:
        return _load_cdhit_clusters_work(filename, headers=True)


def save_cdhit_clusters(filename, cdhit_clusters):
    with open(filename, "w") as tsv:
        for read_id, cluster in cdhit_clusters.items():
            assert cluster != None, f"If this happened, probably dedup1.fa output of cdhit contains reads that are not mentioned in dedup1.fa.clstr.  Perhaps set cluster_size=1 for those reads but also follow up with cdhit.  Read id: {read_id}"
            tsv.write(f"{len(cluster)}\t{read_id}\t{"\t".join(cluster)}\n")
            # TODO: (gdingle): testme


def reads(local_file_path):
    '''
    Count reads in given FASTA or FASTQ file.
    Implemented via wc, so very fast.
    '''
    return _count_reads_via_wc(local_file_path, max_reads=None)


def reads_in_group(file_group, max_fragments=None, cluster_sizes=None, cluster_key=None):
    '''
    OVERVIEW

    Count reads in a group of matching files, up to a maximum number of fragments,
    optionally expanding each fragment to its cdhit cluster size.  Inputs in
    FASTA and FASTQ format are supported, subject to restrictions below.

    DEFINITIONS

    The term "fragment" refers to the physical DNA fragments the reads derive from.
    If the input is single, then 1 fragment == 1 read.
    If the input is paired, then 1 fragment == 2 reads.

    The term "cluster" refers to the output of the pipeline step cd-hit-dup, which
    identifies and groups together duplicate fragments into clusters.  Duplicates
    typically result from PCR or other amplificaiton.  It is cost effective for the
    pipeline to operate on a single representative fragment from each cluster, then
    infer back the original fragment count using the cluster sizes information
    emitted by the cd-hit-dup step.  The caller should load that information via
    m8.load_cluster_sizes() before passing it to this function.

    RESTRICTIONS

    When cluster_sizes is specified, the input must be in FASTA format, and the max_reads
    argument must be unspecified (None).  Thus, the caller should choose between two
    distinct use cases:

       (*) Expand clusters when counting FASTA fragments.

       (*) Count FASTA or FASTQ fragments without expanding clusters,
           optionally truncating to max_fragments.  The input may
           even be gz compressed.

    PERFORMANCE

    When cluster_sizes is not specified, the implementation is very fast, via wc.

    When cluster_sizes is specified, the python implementation can process only about
    3-4 million reads per minute. Fortunately, only steps run_lzw and run_bowtie2 can
    have more than a million fragments, and even that is extremely unlikely (deeply
    sequenced microbiome samples are rare in idseq at this time).  All other steps either
    operate on at most 1 million fragments or do not specify cluster_sizes. If this
    performance ever becomes a concern, the relevant private function can be reimplemented
    easily in GO.

    INTERFACE

    It may be better to expose the two use cases as separate functions, so that users do
    not encounter surprising restrictions or performance differences.  However, that would
    fracture the documentation, obfuscate points of use, and cement a separation that is
    quite arbitrary and forced only by current implementation choices.  Instead, keeping it
    all in one would allow a future reimplementation in a more performant language that
    could lift all restrictions and make all code paths performant.  The choice, then, is
    this better future, and just fail an assert where the present falls short.
    '''
    assert None in (max_fragments, cluster_sizes), "Truncating to max_fragments is not supported at the same time as expanding cluster_sizes.  Consider setting max_fragments=None."
    assert (cluster_sizes == None) == (cluster_key == None), "Please specify cluster_key when using cluster_sizes."
    first_file = file_group[0]
    # This is so fast, just do it always as a sanity check.
    unique_fast = _count_reads_via_wc(first_file, max_fragments)
    if cluster_sizes:
        # Run this even if ReadCountingMode.COUNT_UNIQUE to get it well tested before release.  Dark launch.
        unique, nonunique = _count_reads_expanding_duplicates(first_file, cluster_sizes, cluster_key)
        assert unique_fast == unique, f"Different read counts from wc ({unique_fast}) and fasta.iterator ({unique}) for file {first_file}."
        assert unique <= nonunique, f"Unique count ({unique}) should not exceed nonunique count ({nonunique}) for file {first_file}."
    reads_in_first_file = unique_fast
    if cluster_sizes and READ_COUNTING_MODE == ReadCountingMode.COUNT_ALL:
        reads_in_first_file = nonunique
    num_files = len(file_group)
    return num_files * reads_in_first_file
