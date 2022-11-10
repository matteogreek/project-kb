import logging
import re
import sys
from typing import Dict, List, Set, Tuple

import requests
from tqdm import tqdm

from client.cli.console import ConsoleWriter, MessageStatus
from datamodel.advisory import AdvisoryRecord, build_advisory_record
from datamodel.commit import Commit, apply_ranking, make_from_raw_commit
from filtering.filter import filter_commits
from git.git import Git
from git.raw_commit import RawCommit
from git.version_to_tag import clear_tag, get_possible_tags, get_tag_for_version
from log.logger import get_level, logger, pretty_log
from rules.rules import apply_rules
from stats.execution import (
    Counter,
    ExecutionTimer,
    execution_statistics,
    measure_execution_time,
)

SECS_PER_DAY = 86400
TIME_LIMIT_BEFORE = 3 * 365 * SECS_PER_DAY
TIME_LIMIT_AFTER = 180 * SECS_PER_DAY

MAX_CANDIDATES = 2000
DEFAULT_BACKEND = "http://localhost:8000"


core_statistics = execution_statistics.sub_collection("core")


# @profile
@measure_execution_time(execution_statistics, name="core")
def prospector(  # noqa: C901
    vulnerability_id: str,
    repository_url: str,
    publication_date: str = "",
    vuln_descr: str = "",
    tag_interval: str = "",
    version_interval: str = "",
    modified_files: Set[str] = set(),
    advisory_keywords: Set[str] = set(),
    time_limit_before: int = TIME_LIMIT_BEFORE,
    time_limit_after: int = TIME_LIMIT_AFTER,
    use_nvd: bool = True,
    nvd_rest_endpoint: str = "",
    fetch_references: bool = False,
    backend_address: str = DEFAULT_BACKEND,
    use_backend: str = "always",
    git_cache: str = "/tmp/git_cache",
    limit_candidates: int = MAX_CANDIDATES,
    rules: List[str] = ["ALL"],
) -> Tuple[List[Commit], AdvisoryRecord]:

    logger.debug("begin main commit and CVE processing")

    # construct an advisory record
    with ConsoleWriter("Processing advisory") as _:
        advisory_record = build_advisory_record(
            vulnerability_id,
            vuln_descr,
            nvd_rest_endpoint,
            fetch_references,
            use_nvd,
            publication_date,
            advisory_keywords,
            modified_files,
        )

    # obtain a repository object
    repository = Git(repository_url, git_cache)

    with ConsoleWriter("Git repository cloning") as _:
        logger.info(f"Downloading repository {repository.url} in {repository.path}")
        repository.clone()

        tags = repository.get_tags()

        logger.debug(f"Found tags: {tags}")
        logger.info(f"Done retrieving {repository.url}")

    prev_tag = None
    next_tag = None

    if tag_interval is not None:
        prev_tag, next_tag = tag_interval.split(":")
    elif version_interval is not None:
        prev_tag, next_tag = get_possible_tags(tags, version_interval)
        # vuln_version, fixed_version = version_interval.split(":")
        # prev_tag = get_possible_tags(tags, vuln_version)
        # next_tag = get_possible_tags(tags, fixed_version)
        # prev_tag = get_tag_for_version(tags, vuln_version)[0]
        # next_tag = get_tag_for_version(tags, fixed_version)[0]

    print(f"Found tags: {prev_tag}, {next_tag}") if prev_tag and next_tag else None

    # retrieve of commit candidates
    candidates = get_candidates(
        advisory_record,
        repository,
        prev_tag,
        next_tag,
        time_limit_before,
        time_limit_after,
        limit_candidates,
    )

    with ExecutionTimer(
        core_statistics.sub_collection("commit preprocessing")
    ) as timer:
        with ConsoleWriter("Preprocessing commits") as writer:
            try:
                if use_backend != "never":
                    missing, preprocessed_commits = retrieve_preprocessed_commits(
                        repository_url,
                        backend_address,
                        candidates,
                    )
            except requests.exceptions.ConnectionError:
                logger.error(
                    "Backend not reachable",
                    exc_info=get_level() < logging.WARNING,
                )
                if use_backend == "always":
                    print("Backend not reachable: aborting")
                    sys.exit(0)
                print("Backend not reachable: continuing")

            if "missing" not in locals():
                missing = list(candidates.values())
                preprocessed_commits: List[Commit] = list()

            pbar = tqdm(missing, desc="Preprocessing commits", unit="commit")
            with Counter(
                timer.collection.sub_collection("commit preprocessing")
            ) as counter:
                counter.initialize("preprocessed commits", unit="commit")
                # Now pbar has Raw commits inside so we can skip the "get_commit" call
                for raw_commit in pbar:
                    counter.increment("preprocessed commits")
                    raw_commit.set_tags(next_tag)
                    # TODO: here we need to check twins with the commit not already in the backend and update everything
                    preprocessed_commits.append(make_from_raw_commit(raw_commit))

            pretty_log(logger, advisory_record)
            logger.debug(
                f"preprocessed {len(preprocessed_commits)} commits are only composed of test files"
            )
            payload = [c.to_dict() for c in preprocessed_commits]

    if len(payload) > 0 and use_backend != "never":
        save_preprocessed_commits(backend_address, payload)
    else:
        logger.warning("Preprocessed commits are not being sent to backend")

    # filter commits
    preprocessed_commits = filter(preprocessed_commits)

    # apply rules and rank candidates
    ranked_candidates = evaluate_commits(preprocessed_commits, advisory_record, rules)

    twin_branches_map = {
        commit.commit_id: commit.get_tag() for commit in ranked_candidates
    }
    ranked_candidates = tag_and_aggregate_commits(
        ranked_candidates, twin_branches_map, next_tag
    )
    # TODO: aggregate twins and check tags
    # ranked_candidates = aggregate_twins(ranked_candidates)
    # if next_tag is not None:
    #     tagged_candidates = [
    #         commit
    #         for commit in ranked_candidates
    #         if commit.has_tag()
    #         and (commit.get_tag() in next_tag or next_tag in commit.get_tag())
    #     ]
    #     # This is horrible and slow
    #     for commit in tagged_candidates:
    #         for twin in commit.twins:
    #             twin[0] = twin_branches[twin[1]]

    #     return tagged_candidates, advisory_record

    return ranked_candidates, advisory_record


def filter(commits: List[Commit]) -> List[Commit]:
    with ConsoleWriter("Candidate filtering") as console:
        commits, rejected = filter_commits(commits)
        if rejected > 0:
            console.print(f"Dropped {rejected} candidates")
        return commits


def evaluate_commits(commits: List[Commit], advisory: AdvisoryRecord, rules: List[str]):
    with ExecutionTimer(core_statistics.sub_collection("candidates analysis")):
        with ConsoleWriter("Applying rules"):
            ranked_commits = apply_ranking(apply_rules(commits, advisory, rules=rules))

    return ranked_commits


def tag_and_aggregate_commits(
    commits: List[Commit], mapping_dict: Dict[str, str], next_tag: str
) -> List[Commit]:
    if next_tag is None:
        return commits
    tagged_commits = list()
    for commit in commits:
        if commit.has_tag():
            # if (
            #     commit.commit_id == "ac7ed9580331b8fd65eccd732f651b14f95f71f0"
            #     or commit.commit_id == "8eb68266167d8f8b3fa3a00ca9f6b7889e8ec101"
            #     or commit.commit_id == "1ba43047641e1db238ee15a6fa1a1d8dcd371bc5"
            #     or "(#7359)" in commit.message
            # ):
            #     print(commit.commit_id, commit.get_tag())
            if next_tag == commit.get_tag():
                # cleaned_tag in cleaned_next_tag or cleaned_next_tag in cleaned_tag:
                for twin in commit.twins:
                    twin[0] = mapping_dict[twin[1]]

                tagged_commits.append(commit)

    return tagged_commits

    # if next_tag is not None:
    #     tagged_candidates = [
    #         commit
    #         for commit in ranked_candidates
    #         if commit.has_tag()
    #         and (commit.get_tag() in next_tag or next_tag in commit.get_tag())
    #     ]
    #     # This is horrible and slow
    #     for commit in tagged_candidates:
    #         for twin in commit.twins:
    #             twin[0] = twin_branches[twin[1]]

    #     return tagged_candidates, advisory_record

    # return aggregated_commits


def retrieve_preprocessed_commits(
    repository_url: str, backend_address: str, candidates: Dict[str, RawCommit]
) -> Tuple[List[RawCommit], List[Commit]]:
    retrieved_commits: List[dict] = list()
    missing: List[RawCommit] = list()

    responses = list()
    for i in range(0, len(candidates), 500):
        args = list(candidates.keys())[i : i + 500]
        r = requests.get(
            f"{backend_address}/commits/{repository_url}?commit_id={','.join(args)}"
        )
        if r.status_code != 200:
            logger.info("One or more commits are not in the backend")
            break  # return list(candidates.values()), list()
        responses.append(r.json())

    retrieved_commits = [commit for response in responses for commit in response]

    logger.info(f"Found {len(retrieved_commits)} preprocessed commits")

    if len(retrieved_commits) != len(candidates):
        missing = [
            candidates[c]
            for c in set(candidates.keys()).difference(
                rc["commit_id"] for rc in retrieved_commits
            )
        ]

        logger.error(f"Missing {len(missing)} commits")

    return missing, [Commit.parse_obj(rc) for rc in retrieved_commits]


def save_preprocessed_commits(backend_address, payload):
    with ExecutionTimer(core_statistics.sub_collection(name="save commits to backend")):
        with ConsoleWriter("Saving preprocessed commits to backend") as writer:
            logger.debug("Sending preprocessing commits to backend...")
            try:
                r = requests.post(
                    backend_address + "/commits/",
                    json=payload,
                    headers={"Content-type": "application/json"},
                )
                logger.debug(
                    f"Saving to backend completed (status code: {r.status_code})"
                )
            except requests.exceptions.ConnectionError:
                logger.error(
                    "Could not reach backend, is it running?"
                    "The result of commit pre-processing will not be saved."
                    "Continuing anyway.....",
                    exc_info=get_level() < logging.WARNING,
                )
                writer.print(
                    "Could not save preprocessed commits to backend",
                    status=MessageStatus.WARNING,
                )


def get_candidates(
    advisory_record: AdvisoryRecord,
    repository: Git,
    prev_tag: str,
    next_tag: str,
    time_limit_before: int,
    time_limit_after: int,
    limit_candidates: int,
):
    with ExecutionTimer(
        core_statistics.sub_collection(name="retrieval of commit candidates")
    ):

        with ConsoleWriter("Candidate commit retrieval") as writer:

            since = None
            until = None
            if advisory_record.published_timestamp:
                since = advisory_record.published_timestamp - time_limit_before
                until = advisory_record.published_timestamp + time_limit_after
            # Here i need to strip the github tags of useless stuff
            # This is now a list of raw commits
            # TODO: get_commits replaced for now
            candidates = repository.create_commits(
                since=since,
                until=until,
                ancestors_of=next_tag,
                exclude_ancestors_of=prev_tag,
            )

            core_statistics.record("candidates", len(candidates), unit="commits")
            logger.info("Found %d candidates" % len(candidates))
        writer.print(f"Found {len(candidates)} candidates")

        if len(candidates) > limit_candidates:
            logger.error(f"Number of candidates exceeds {limit_candidates}, aborting.")

            writer.print(
                f"Found {len(candidates)} candidates, too many to proceed.",
                status=MessageStatus.ERROR,
            )
            writer.print("Please try running the tool again.")
            sys.exit(-1)

    return candidates
