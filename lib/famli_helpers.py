#!/usr/bin/python

import os
import gzip
import json
import logging
import argparse
import numpy as np
from multiprocessing import Pool
from collections import defaultdict


class BLAST6Parser:
    """Object to help with parsing alignments in BLAST 6 format."""

    def __init__(self):
        # Keep track of the length of all subjects
        self.subject_len = {}
        # Keep track of the number of queries
        self.unique_queries = set([])

    def parse(self,
              align_handle,
              QSEQID_i=0,
              SSEQID_i=1,
              SSTART_i=8,
              SEND_i=9,
              EVALUE_i=10,
              BITSCORE_i=11,
              SLEN_i=13):
        """Parse a file, while keeping track of the subject lengths."""
        logging.info("Reading in alignments")
        for i, line in enumerate(align_handle):
            if i % 1000000 == 0 and i > 0:
                logging.info("{:,} lines of alignment parsed".format(i))
            line_list = line.strip().split()
            # Get the query and subject
            query = line_list[QSEQID_i]
            subject = line_list[SSEQID_i]
            bitscore = float(line_list[BITSCORE_i])
            sstart = int(line_list[SSTART_i])
            send = int(line_list[SEND_i])

            # Save information for the subject length
            self.subject_len[subject] = int(line_list[SLEN_i])

            # Add the query to the set of unique queries
            self.unique_queries.add(query)

            # Yield a tuple with query, subject, sstart, send, bitscore
            yield (
                query,
                subject,
                sstart - 1,  # Convert to 0-based indexing
                send,
                bitscore
            )

        logging.info("Done reading in {:,} alignments".format(i + 1))
        logging.info("Number of unique subjects: {:,}".format(
            len(self.subject_len)))
        logging.info("Number of unique queries: {:,}".format(
            len(self.unique_queries)))


class FAMLI_Reassignment:
    """Object to help with parsing alignments in BLAST 6 format."""

    def __init__(self, alignments, subject_len):
        # Save the length of all subjects
        self.subject_len = subject_len

        # Keep track of the bitscores for each query/subject
        self.bitscores = defaultdict(lambda: defaultdict(float))

        # Keep track of the number of uniquely aligned queries
        self.n_unique = 0

        logging.info("Adding alignments to read re-assignment model")
        for query, subject, sstart, send, bitscore in alignments:
            # Save information for the query and subject
            self.bitscores[query][subject] = bitscore

    def init_subject_weight(self):
        """Initialize the subject weights, all being equal."""
        logging.info("Initializing subject weights")
        self.subject_weight = {
            subject: 1 / length
            for subject, length in self.subject_len.items()
        }

        # Also initialize the alignment probabilities as the bitscores
        logging.info("Initializing alignment probabilities")
        self.aln_prob = defaultdict(dict)
        self.aln_prob_T = defaultdict(dict)

        # Keep track of the queries and subjects that will need to be updated
        self.subjects_to_update = set([])
        self.queries_to_update = set([])

        for query, bitscores in self.bitscores.items():
            self.queries_to_update.add(query)
            if len(bitscores) == 1:
                self.n_unique += 1
            bitscore_sum = sum(bitscores.values())
            for subject, bitscore in bitscores.items():
                self.subjects_to_update.add(subject)
                v = bitscore / bitscore_sum
                self.aln_prob[query][subject] = v
                self.aln_prob_T[subject][query] = v

    def recalc_subject_weight(self):
        """Recalculate the subject weights."""
        self.queries_to_update = set([])

        for subject, length in self.subject_len.items():
            if subject in self.subjects_to_update:
                self.subject_weight[subject] = sum(self.aln_prob_T[subject].values()) / length
                self.queries_to_update |= set(self.aln_prob_T[subject].keys())

    def recalc_aln_prob(self):
        """Recalculate the alignment probabilities."""
        self.subjects_to_update = set([])

        # Iterate over every query
        for query, aln_prob in self.aln_prob.items():
            if query in self.queries_to_update:
                new_probs = [
                    prob * self.subject_weight[subject]
                    for subject, prob in aln_prob.items()
                ]
                new_probs_sum = sum(new_probs)
                for ix, subject in enumerate(aln_prob):
                    self.subjects_to_update.add(subject)
                    v = new_probs[ix] / new_probs_sum
                    self.aln_prob[query][subject] = v
                    self.aln_prob_T[subject][query] = v

    def trim_least_likely(self, cutoff=0.25):
        """Remove the least likely alignments."""
        n_trimmed = 0
        for query, aln_prob in self.aln_prob.items():
            # Skip queries with only a single possible subject
            if len(aln_prob) == 1:
                continue
            # Find the best likelihood value to trim
            max_p = np.median(list(aln_prob.values()))
            if max_p > cutoff:
                least_likely = cutoff
            else:
                least_likely = max_p

            to_remove = [
                subject for subject, prob in aln_prob.items()
                if prob <= least_likely
            ]
            # Don't remove all of the subjects
            if len(to_remove) == len(aln_prob):
                continue

            n_trimmed += len(to_remove)

            # Remove the subjects
            for subject in to_remove:
                del self.aln_prob[query][subject]
                del self.aln_prob_T[subject][query]

            if len(self.aln_prob[query]) == 1:
                self.n_unique += 1

        logging.info("Removed {:,} unlikely alignments".format(n_trimmed))
        logging.info("Number of uniquely aligned queries: {:,}".format(
            self.n_unique))
        return n_trimmed


def filter_subjects_by_coverage(args):
    """Check whether the subject passes the coverage filter."""

    alignments, subject_len, SD_MEAN_CUTOFF, STRIM_5, STRIM_3 = args

    # Make a coverage vector
    cov = np.zeros(subject_len, dtype=int)

    # Add the alignments
    for query, subject, sstart, send, bitscore in alignments:
        cov[sstart: send] += 1

    # Trim the ends
    if subject_len >= STRIM_5 + STRIM_3 + 10:
        cov = cov[STRIM_5: -STRIM_3]

    return cov.mean() > 0 and cov.std() / cov.mean() <= SD_MEAN_CUTOFF


def group_aln_by_subject(alignments):
    """Group a set of sorted alignments by subject."""
    output = []
    buff = []
    last_subject = None
    for a in alignments:
        if a[1] != last_subject:
            if last_subject is not None:
                output.append([last_subject, buff])
            buff = []
            last_subject = a[1]
        buff.append(a)
    output.append([last_subject, buff])
    return output


def parse_alignment(align_handle,
                    QSEQID_i=0,
                    SSEQID_i=1,
                    QSTART_i=6,
                    QEND_i=7,
                    SSTART_i=8,
                    SEND_i=9,
                    BITSCORE_i=11,
                    SLEN_i=13,
                    SD_MEAN_CUTOFF=2.0,
                    STRIM_5=18,
                    STRIM_3=18,
                    threads=4):
    """
    Parse an alignment in BLAST6 format and determine which subjects are likely to be present. This is the core of FAMLI.
    BLAST 6 columns by default (in order): qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore qlen slen
                                              0     1       2       3       4       5       6   7   8       9   10      11      12   13  
    """

    # Initialize the alignment parser
    parser = BLAST6Parser()

    # Read in the alignments
    alignments = [a for a in parser.parse(align_handle)]

    # Sort alignments by subject
    logging.info("Sorting alignments by subject")
    alignments.sort(key=lambda a: a[1])

    logging.info("Grouping alignments by subject")
    alignments = group_aln_by_subject(alignments)

    # STEP 1. FILTER SUBJECTS BY "COULD" COVERAGE EVENNESS
    pool = Pool(threads)
    logging.info("FILTER 1: Even coverage of all alignments")
    filter_1 = pool.map(filter_subjects_by_coverage, [
        [
            subject_aln,
            parser.subject_len[subject],
            SD_MEAN_CUTOFF,
            STRIM_5,
            STRIM_3
        ]
        for subject, subject_aln in alignments
    ])

    logging.info("Subjects passing FILTER 1: {:,} / {:,}".format(
        sum(filter_1), len(filter_1)
    ))

    # Subset the alignments
    alignments = [
        a for passes_filter, a in zip(filter_1, alignments)
        if passes_filter
    ]
    # Flatten the alignments into a single list
    alignments = [
        a
        for subject, subject_aln in alignments
        for a in subject_aln
    ]

    logging.info("Queries passing FILTER 1: {:,} / {:,}".format(
        len(set([a[0] for a in alignments])), len(parser.unique_queries)
    ))

    # STEP 2: Reassign multi-mapped reads to a single subject
    logging.info("FILTER 2: Reassign queries to a single subject")

    # Add the alignments to a model to optimally re-assign reads
    model = FAMLI_Reassignment(alignments, parser.subject_len)

    # Initialize the subject weights
    model.init_subject_weight()

    ix = 0
    while True:
        ix += 1
        logging.info("Iteration: {:,}".format(ix))
        # Recalculate the subject weight, given the naive alignment probabliities
        model.recalc_subject_weight()
        # Recalculate the alignment probabilities, given the subject weights
        model.recalc_aln_prob()

        # Trim the least likely alignment for each read
        n_trimmed = model.trim_least_likely()

        if n_trimmed == 0:
            break

    # Subset the alignment to only the reassigned queries
    alignments = [
        (query, subject, sstart, send, bitscore)
        for query, subject, sstart, send, bitscore in alignments
        if subject in model.aln_prob[query] and
        len(model.aln_prob[query]) == 1
    ]

    logging.info("Finished reassigning reads ({:,} remaining)".format(
        len(alignments)))

    # STEP 3: Filter subjects by even coverage of reassigned queries
    logging.info("FILTER 3: Filtering subjects by even sequencing coverage")

    alignments = group_aln_by_subject(alignments)

    filter_3 = pool.map(filter_subjects_by_coverage, [
        [
            subject_aln,
            parser.subject_len[subject],
            SD_MEAN_CUTOFF,
            STRIM_5,
            STRIM_3
        ]
        for subject, subject_aln in alignments
    ])

    logging.info("Subjects passing FILTER 3: {:,} / {:,}".format(
        sum(filter_3), len(filter_3)
    ))

    # Subset the alignments
    alignments = [
        a for passes_filter, a in zip(filter_3, alignments)
        if passes_filter
    ]
    # Flatten the alignments into a single list
    alignments = [
        a
        for subject, subject_aln in alignments
        for a in subject_aln
    ]

    # Make the output by calculating coverage per subject
    output = []

    # Make a dict of the alignment ranges
    logging.info("Collecting final alignments")
    alignment_ranges = defaultdict(list)
    for query, subject, sstart, send, bitscore in alignments:
        # Alignment was removed
        if subject not in model.aln_prob[query]:
            continue
        # Alignment is not unique
        elif len(model.aln_prob[query]) > 1:
            continue
        alignment_ranges[subject].append((sstart, send))

    logging.info("Calculating final stats")

    for subject, aln_ranges in alignment_ranges.items():
        # Make a coverage map
        cov = np.zeros(model.subject_len[subject], dtype=int)

        # Add to the coverage
        for sstart, send in aln_ranges:
            cov[sstart:send] += 1

        output.append({
            "id": subject,
            "nreads": len(aln_ranges),
            "coverage": (cov > 0).mean(),
            "depth": cov.mean(),
            "std": cov.std(),
            "length": model.subject_len[subject],
        })

    logging.info("Results: assigned {:,} queries to {:,} subjects".format(
        sum([d["nreads"] for d in output]),
        len(output),
    ))

    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="""
    Parse a DIAMOND output file and save the deduplicated coverage data.
    """)
    parser.add_argument("--input",
                        type=str,
                        required=True,
                        help="""DIAMOND output file in tabular format.""")
    parser.add_argument("--output",
                        type=str,
                        help="""Output file in JSON format.""")
    parser.add_argument("--threads",
                        type=int,
                        help="""Number of processors to use.""",
                        default=4)

    args = parser.parse_args()

    assert os.path.exists(args.input)

    # Set up logging
    logFormatter = logging.Formatter(
        '%(asctime)s %(levelname)-8s [FAMLI Parser] %(message)s'
    )
    rootLogger = logging.getLogger()
    rootLogger.setLevel(logging.INFO)

    # Write to STDOUT
    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(logFormatter)
    rootLogger.addHandler(consoleHandler)

    if args.input.endswith(".gz"):
        with gzip.open(args.input, "rt") as f:
            output = parse_alignment(f, threads=args.threads)
    else:
        with open(args.input, "rt") as f:
            output = parse_alignment(f, threads=args.threads)

    if args.output:
        with open(args.output, "wt") as fo:
            json.dump(output, fo)
