import json
import os
import random
import time
from itertools import product
from pathlib import Path

from inputimeout import inputimeout, TimeoutOccurred
from loguru import logger
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import src.utils as utils
from app_config import MINIMUM_WAIT_TIME
from src.aihawk_easy_applier import AIHawkEasyApplier
from src.job import Job
import urllib.parse


class EnvironmentKeys:
    def __init__(self):
        logger.debug("Initializing EnvironmentKeys")
        self.skip_apply = self._read_env_key_bool("SKIP_APPLY")
        self.disable_description_filter = self._read_env_key_bool(
            "DISABLE_DESCRIPTION_FILTER"
        )
        logger.debug(
            "EnvironmentKeys initialized: "
            f"skip_apply={self.skip_apply}, "
            f"disable_description_filter={self.disable_description_filter}"
        )

    @staticmethod
    def _read_env_key(key: str) -> str:
        value = os.getenv(key, "")
        logger.debug(f"Read environment key {key}: {value}")
        return value

    @staticmethod
    def _read_env_key_bool(key: str) -> bool:
        value = os.getenv(key) == "True"
        logger.debug(f"Read environment key {key} as bool: {value}")
        return value


class AIHawkJobManager:
    """
    High-level manager for:
      • navigating LinkedIn jobs search,
      • parsing job cards from the (new) HTML,
      • filtering / blacklist logic,
      • invoking AIHawkEasyApplier on Easy Apply jobs,
      • writing results to JSON files.
    """

    def __init__(self, driver):
        logger.debug("Initializing AIHawkJobManager")
        self.driver = driver
        self.set_old_answers = set()
        self.easy_applier_component = None
        self.env_config = EnvironmentKeys()
        logger.debug("AIHawkJobManager initialized successfully")

    # -------------------------------------------------------------------------
    # Dependency injection
    # -------------------------------------------------------------------------

    def set_parameters(self, parameters):
        logger.debug("Setting parameters for AIHawkJobManager")

        self.company_blacklist = parameters.get("company_blacklist", []) or []
        self.title_blacklist = parameters.get("title_blacklist", []) or []
        self.positions = parameters.get("positions", [])
        self.locations = parameters.get("locations", [])
        self.apply_once_at_company = parameters.get("apply_once_at_company", False)
        self.base_search_url = self.get_base_search_url(parameters)
        self.seen_jobs = []

        job_applicants_threshold = parameters.get("job_applicants_threshold", {})
        self.min_applicants = job_applicants_threshold.get("min_applicants", 0)
        self.max_applicants = job_applicants_threshold.get(
            "max_applicants", float("inf")
        )

        resume_path = parameters.get("uploads", {}).get("resume")
        if resume_path and Path(resume_path).exists():
            self.resume_path = Path(resume_path)
        else:
            self.resume_path = None

        self.output_file_directory = Path(parameters["outputFileDirectory"])

        logger.debug(
            "Parameters set successfully: "
            f"positions={self.positions}, locations={self.locations}, "
            f"apply_once_at_company={self.apply_once_at_company}, "
            f"base_search_url={self.base_search_url}"
        )

    def set_gpt_answerer(self, gpt_answerer):
        logger.debug("Setting GPT answerer")
        self.gpt_answerer = gpt_answerer

    def set_resume_generator_manager(self, resume_generator_manager):
        logger.debug("Setting resume generator manager")
        self.resume_generator_manager = resume_generator_manager

    # -------------------------------------------------------------------------
    # Internal helpers for the new LinkedIn HTML
    # -------------------------------------------------------------------------

    def _get_results_list_container(self):
        """
        Try to find the <ul> that holds job search results under the new
        LinkedIn UI.

        We use multiple fallbacks because LinkedIn keeps renaming classes.
        """
        wait = WebDriverWait(self.driver, 15)

        # 1) New scaffold layout container
        try:
            outer = wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "div.scaffold-layout__list")
                )
            )
            uls = outer.find_elements(By.TAG_NAME, "ul")
            for ul in uls:
                if ul.find_elements(By.CSS_SELECTOR, "li.scaffold-layout__list-item"):
                    logger.debug("Found results <ul> under .scaffold-layout__list")
                    return ul
        except Exception as e:
            logger.debug(f"No UL in .scaffold-layout__list: {e}")

        # 2) Older / alternative jobs results list
        try:
            ul = wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "ul.jobs-search-results__list")
                )
            )
            logger.debug("Found results <ul> .jobs-search-results__list")
            return ul
        except Exception as e:
            logger.debug(f"No .jobs-search-results__list: {e}")

        # 3) Fallback – any UL with scaffold list items
        try:
            all_uls = self.driver.find_elements(By.TAG_NAME, "ul")
            for ul in all_uls:
                if ul.find_elements(By.CSS_SELECTOR, "li.scaffold-layout__list-item"):
                    logger.debug("Found results <ul> via generic UL fallback")
                    return ul
        except Exception as e:
            logger.error(f"Error in UL fallback search: {e}")

        logger.debug("No job results list container found")
        return None

    def _get_job_tiles(self):
        """
        Get the list of <li> elements that actually contain job cards.

        We filter by requiring a recognizable job card wrapper, because
        LinkedIn inserts a lot of ghost/occludable elements.
        """
        # Check "no results" banner first (if still present)
        try:
            no_jobs_element = self.driver.find_element(
                By.CLASS_NAME, "jobs-search-two-pane__no-results-banner--expand"
            )
            text = (no_jobs_element.text or "").lower()
            page_src = self.driver.page_source.lower()
            if (
                "no matching jobs found" in text
                or "unfortunately, things aren" in page_src
            ):
                logger.debug("No matching jobs found banner detected.")
                return []
        except NoSuchElementException:
            pass

        ul = self._get_results_list_container()
        if not ul:
            logger.debug("No job results list container available.")
            return []

        # Scroll the list to force lazy-loading of all cards
        try:
            utils.scroll_slow(self.driver, ul)
            utils.scroll_slow(self.driver, ul, step=300, reverse=True)
        except Exception as e:
            logger.debug(f"scroll_slow failed (non-fatal): {e}")

        # LinkedIn uses scaffold list items for each job
        li_elements = ul.find_elements(
            By.CSS_SELECTOR,
            "li.scaffold-layout__list-item, li.jobs-search-results__list-item",
        )
        if not li_elements:
            logger.debug("No list items found for job cards.")
            return []

        job_tiles = []
        for li in li_elements:
            try:
                # New + old card wrappers
                has_card = li.find_elements(
                    By.CSS_SELECTOR,
                    "div.job-card-container, "
                    "div.job-card-job-posting-card-wrapper",
                )
                if has_card:
                    job_tiles.append(li)
            except Exception:
                continue

        if not job_tiles:
            logger.debug("No job card wrappers detected inside list items.")
        else:
            logger.debug(f"Found {len(job_tiles)} candidate job tiles")

        return job_tiles

    # -------------------------------------------------------------------------
    # High-level flows
    # -------------------------------------------------------------------------

    def start_collecting_data(self):
        """
        Only reads job metadata & writes JSON (does NOT apply).
        """
        searches = list(product(self.positions, self.locations))
        random.shuffle(searches)

        page_sleep = 0
        minimum_time = 60 * 5
        minimum_page_time = time.time() + minimum_time

        for position, location in searches:
            location_url = "&location=" + urllib.parse.quote(location)
            job_page_number = -1
            utils.printyellow(f"Collecting data for {position} in {location}.")

            try:
                while True:
                    page_sleep += 1
                    job_page_number += 1
                    utils.printyellow(f"Going to job page {job_page_number}")
                    self.next_job_page(position, location_url, job_page_number)
                    time.sleep(random.uniform(1.5, 3.5))
                    utils.printyellow("Starting the collecting process for this page")

                    self.read_jobs()
                    utils.printyellow("Collecting data on this page has been completed!")

                    time_left = minimum_page_time - time.time()
                    if time_left > 0:
                        utils.printyellow(f"Sleeping for {time_left} seconds.")
                        time.sleep(time_left)
                        minimum_page_time = time.time() + minimum_time

                    if page_sleep % 5 == 0:
                        sleep_time = random.randint(1, 5)
                        utils.printyellow(f"Sleeping for {sleep_time / 60} minutes.")
                        time.sleep(sleep_time)
                        page_sleep += 1
            except Exception as e:
                logger.error(f"Error while collecting data: {e}")

            # After finishing this (position, location) search
            time_left = minimum_page_time - time.time()
            if time_left > 0:
                utils.printyellow(f"Sleeping for {time_left} seconds.")
                time.sleep(time_left)
                minimum_page_time = time.time() + minimum_time

            if page_sleep % 5 == 0:
                sleep_time = random.randint(50, 90)
                utils.printyellow(f"Sleeping for {sleep_time / 60} minutes.")
                time.sleep(sleep_time)
                page_sleep += 1

    def start_applying(self):
        """
        Full apply loop – iterates over (position, location) searches and
        uses AIHawkEasyApplier on Easy Apply jobs that match your keyword
        filters and blacklist rules.
        """
        logger.debug("Starting job application process")
        self.easy_applier_component = AIHawkEasyApplier(
            self.driver,
            self.resume_path,
            self.set_old_answers,
            self.gpt_answerer,
            self.resume_generator_manager,
        )

        searches = list(product(self.positions, self.locations))
        random.shuffle(searches)

        page_sleep = 0
        minimum_time = MINIMUM_WAIT_TIME
        minimum_page_time = time.time() + minimum_time

        for position, location in searches:
            location_url = "&location=" + urllib.parse.quote(location)
            job_page_number = -1
            logger.debug(f"Starting the search for {position} in {location}.")

            try:
                while True:
                    page_sleep += 1
                    job_page_number += 1
                    logger.debug(f"Going to job page {job_page_number}")
                    self.next_job_page(position, location_url, job_page_number)
                    time.sleep(random.uniform(1.5, 3.5))
                    logger.debug("Starting the application process for this page...")

                    try:
                        tiles = self.get_jobs_from_page()
                        if not tiles:
                            logger.debug(
                                "No more jobs found on this page. Exiting page loop."
                            )
                            break
                    except Exception as e:
                        logger.error(f"Failed to retrieve jobs: {e}")
                        break

                    try:
                        self.apply_jobs()
                    except Exception as e:
                        logger.error(f"Error during job application: {e}")
                        # continue to next page / search
                        continue

                    logger.debug(
                        "Finished attempting applications for jobs on this page."
                    )

                    # Respect minimum page time, allow user to skip
                    time_left = minimum_page_time - time.time()
                    if time_left > 0:
                        try:
                            user_input = inputimeout(
                                prompt=(
                                    f"Sleeping for {time_left:.1f} seconds. "
                                    f"Press 'y' to skip waiting (timeout 60s): "
                                ),
                                timeout=60,
                            ).strip().lower()
                        except TimeoutOccurred:
                            user_input = ""

                        if user_input == "y":
                            logger.debug("User chose to skip waiting.")
                        else:
                            logger.debug(
                                f"Sleeping for {time_left:.1f} seconds "
                                "as user chose not to skip."
                            )
                            time.sleep(time_left)

                    minimum_page_time = time.time() + minimum_time

                    if page_sleep % 5 == 0:
                        sleep_time = random.randint(5, 34)
                        try:
                            user_input = inputimeout(
                                prompt=(
                                    f"Sleeping for {sleep_time / 60:.1f} minutes. "
                                    f"Press 'y' to skip waiting (timeout 60s): "
                                ),
                                timeout=60,
                            ).strip().lower()
                        except TimeoutOccurred:
                            user_input = ""

                        if user_input == "y":
                            logger.debug("User chose to skip waiting.")
                        else:
                            logger.debug(f"Sleeping for {sleep_time} seconds.")
                            time.sleep(sleep_time)
                        page_sleep += 1

            except Exception as e:
                logger.error(f"Unexpected error during job search loop: {e}")
                continue

            # After finishing this (position, location) search
            time_left = minimum_page_time - time.time()
            if time_left > 0:
                try:
                    user_input = inputimeout(
                        prompt=(
                            f"Sleeping for {time_left:.1f} seconds. "
                            f"Press 'y' to skip waiting (timeout 60s): "
                        ),
                        timeout=60,
                    ).strip().lower()
                except TimeoutOccurred:
                    user_input = ""

                if user_input == "y":
                    logger.debug("User chose to skip waiting.")
                else:
                    logger.debug(
                        f"Sleeping for {time_left:.1f} seconds "
                        "as user chose not to skip."
                    )
                    time.sleep(time_left)

            minimum_page_time = time.time() + minimum_time

            if page_sleep % 5 == 0:
                sleep_time = random.randint(50, 90)
                try:
                    user_input = inputimeout(
                        prompt=(
                            f"Sleeping for {sleep_time / 60:.1f} minutes. "
                            f"Press 'y' to skip waiting (timeout 60s): "
                        ),
                        timeout=60,
                    ).strip().lower()
                except TimeoutOccurred:
                    user_input = ""

                if user_input == "y":
                    logger.debug("User chose to skip waiting.")
                else:
                    logger.debug(f"Sleeping for {sleep_time} seconds.")
                    time.sleep(sleep_time)
                page_sleep += 1

    # -------------------------------------------------------------------------
    # Page parsing based on new HTML
    # -------------------------------------------------------------------------

    def get_jobs_from_page(self):
        """
        Return raw job tile elements for the current page.

        Used by start_applying() just to know if the page has any jobs left.
        """
        try:
            tiles = self._get_job_tiles()
            return tiles
        except Exception as e:
            logger.error(f"Error while fetching job elements: {e}")
            return []

    def check_job_title(self, job_title: str) -> bool:
        """
        Simple keyword filter for titles we care about.
        """
        keywords = [
            "senior machine learning engineer",
            "machine learning engineer",
            "ai engineer",
            "ai/ml",
            "ml",
            "ai",
            "python",
            "llm",
            "data science",
            "data scientist",
            "data engineer",
            "high frequency trading",
            "quant trading",
            "quant developer",
            "lead ml engineer",
            "data engineer python llm",
            "senior machine learning engineer",
        ]

        job_title_lower = (job_title or "").lower()
        return any(keyword.lower() in job_title_lower for keyword in keywords)

    def read_jobs(self):
        """
        Collect job data from the current page and write to output files.
        Used during the data collection phase (not applying).
        """
        # Banner check (if still exists)
        try:
            no_jobs_element = self.driver.find_element(
                By.CLASS_NAME, "jobs-search-two-pane__no-results-banner--expand"
            )
            text = (no_jobs_element.text or "").lower()
            if (
                "no matching jobs found" in text
                or "unfortunately, things aren" in self.driver.page_source.lower()
            ):
                raise Exception("No more jobs on this page")
        except NoSuchElementException:
            pass

        job_tiles = self._get_job_tiles()
        if not job_tiles:
            raise Exception("No job tiles found on page")

        job_list = []
        for tile in job_tiles:
            (
                job_title,
                company,
                job_location,
                link,
                apply_method,
            ) = self.extract_job_information_from_tile(tile)
            if not job_title or not link:
                logger.debug("Skipping list item without job title or link")
                continue
            job_list.append(Job(job_title, company, job_location, link, apply_method))

        for job in job_list:
            if self.is_blacklisted(job.title, job.company, job.link):
                utils.printyellow(
                    f"Blacklisted {job.title} at {job.company}, skipping..."
                )
                self.write_to_file(job, "skipped")
                continue

            try:
                self.write_to_file(job, "data")
            except Exception as e:
                logger.error(
                    f"Failed writing job data for {job.title} at {job.company}: {e}"
                )
                self.write_to_file(job, "failed")

    def apply_jobs(self):
        """
        Iterate through jobs on the current page and apply using Easy Apply.
        """
        # Check "no results" banner
        try:
            no_jobs_element = self.driver.find_element(
                By.CLASS_NAME, "jobs-search-two-pane__no-results-banner--expand"
            )
            text = (no_jobs_element.text or "").lower()
            if (
                "no matching jobs found" in text
                or "unfortunately, things aren" in self.driver.page_source.lower()
            ):
                logger.debug("No matching jobs found on this page, skipping")
                return
        except NoSuchElementException:
            pass

        job_tiles = self._get_job_tiles()
        if not job_tiles:
            logger.debug("No job tiles found on page, skipping")
            return

        job_list = []
        for tile in job_tiles:
            (
                job_title,
                company,
                job_location,
                link,
                apply_method,
            ) = self.extract_job_information_from_tile(tile)
            if not job_title or not link:
                logger.debug("Skipping list item without job title or link")
                continue
            job_list.append(Job(job_title, company, job_location, link, apply_method))

        for job in job_list:
            logger.debug(f"Considering job: {job.title} at {job.company}")

            if self.is_blacklisted(job.title, job.company, job.link):
                logger.debug(f"Job blacklisted: {job.title} at {job.company}")
                self.write_to_file(job, "skipped")
                continue

            if self.is_already_applied_to_job(job.title, job.company, job.link):
                self.write_to_file(job, "skipped")
                continue

            if self.is_already_applied_to_company(job.company):
                self.write_to_file(job, "skipped")
                continue

            # Only attempt Easy Apply jobs whose title passes keyword filter
            if job.apply_method != "Easy Apply":
                logger.debug(
                    f"Not an Easy Apply job (apply_method={job.apply_method}) – skipping"
                )
                self.write_to_file(job, "skipped")
                continue

            if not self.check_job_title(job.title):
                utils.printyellow(
                    f"Job title keywords didn't match for "
                    f"{job.title} at {job.company}, skipping..."
                )
                self.write_to_file(job, "skipped")
                continue

            try:
                logger.debug(f"Attempting Easy Apply for {job.title} at {job.company}")
                self.easy_applier_component.job_apply(job)
                self.write_to_file(job, "success")
                logger.debug(f"Applied to job: {job.title} at {job.company}")
            except Exception as e:
                logger.error(f"Failed to apply for {job.title} at {job.company}: {e}")
                self.write_to_file(job, "failed")

    # -------------------------------------------------------------------------
    # IO, URL construction, and helpers
    # -------------------------------------------------------------------------

    def write_to_file(self, job, file_name):
        """
        Append job info to <file_name>.json in output directory.

        Safely handles missing / empty job.pdf_path.
        """
        logger.debug(f"Writing job application result to file: {file_name}")

        # pdf_path is optional – only convert to URI if present
        pdf_uri = ""
        raw_pdf_path = getattr(job, "pdf_path", "") or ""
        if raw_pdf_path:
            try:
                pdf_uri = Path(raw_pdf_path).resolve().as_uri()
            except Exception as e:
                logger.debug(f"Could not resolve pdf_path '{raw_pdf_path}': {e}")
                pdf_uri = ""

        data = {
            "company": job.company,
            "job_title": job.title,
            "link": job.link,
            "job_recruiter": getattr(job, "recruiter_link", ""),
            "job_location": job.location,
            "pdf_path": pdf_uri,
        }

        file_path = self.output_file_directory / f"{file_name}.json"
        if not file_path.exists():
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump([data], f, indent=4)
            logger.debug(f"Job data written to new file: {file_name}")
        else:
            with open(file_path, "r+", encoding="utf-8") as f:
                try:
                    existing_data = json.load(f)
                except json.JSONDecodeError:
                    logger.error(f"JSON decode error in file: {file_path}")
                    existing_data = []
                existing_data.append(data)
                f.seek(0)
                json.dump(existing_data, f, indent=4)
                f.truncate()
            logger.debug(f"Job data appended to existing file: {file_name}")

    def get_base_search_url(self, parameters):
        """
        Construct the base query string for LinkedIn jobs search (without
        keywords/location/start), using the same semantics you had before but
        cleaned up slightly.
        """
        logger.debug("Constructing base search URL")
        url_parts = []

        # Remote filter
        if parameters.get("remote"):
            url_parts.append("f_CF=f_WRA")

        # Experience levels (1..N according to LinkedIn's internal values)
        experience_levels = [
            str(i + 1)
            for i, (level, v) in enumerate(
                parameters.get("experience_level", {}).items()
            )
            if v
        ]
        if experience_levels:
            url_parts.append(f"f_E={','.join(experience_levels)}")

        # Distance
        url_parts.append(f"distance={parameters['distance']}")

        # Job types (F/P/C/T etc)
        job_types = [
            key[0].upper()
            for key, value in parameters.get("jobTypes", {}).items()
            if value
        ]
        if job_types:
            url_parts.append(f"f_JT={','.join(job_types)}")

        # Date posted
        date_mapping = {
            "all time": "",
            "month": "&f_TPR=r2592000",
            "week": "&f_TPR=r604800",
            "24 hours": "&f_TPR=r86400",
        }
        date_param = next(
            (v for k, v in date_mapping.items() if parameters.get("date", {}).get(k)),
            "",
        )

        # Easy Apply filter
        url_parts.append("f_LF=f_AL")

        base_url = "&".join(url_parts)
        full_url = f"?{base_url}{date_param}"
        logger.debug(f"Base search URL constructed: {full_url}")
        return full_url

    def next_job_page(self, position, location, job_page):
        """
        Navigate to the next job page.

        :param position: job position string (will be URL encoded here)
        :param location: pre-built location param string, e.g. '&location=Toronto%2C%20Ontario'
        :param job_page: page index (0-based)
        """
        logger.debug(
            f"Navigating to next job page: position='{position}', "
            f"location='{location}', page={job_page}"
        )
        encoded_position = urllib.parse.quote(position)
        self.driver.get(
            "https://www.linkedin.com/jobs/search/"
            f"{self.base_search_url}&keywords={encoded_position}"
            f"{location}&start={job_page * 25}"
        )

    def extract_job_information_from_tile(self, job_tile):
        """
        Extract title, company, location, link, and apply_method from a single
        <li> job card in a way that works with both the old and the new
        LinkedIn layouts.
        """
        logger.debug("Extracting job information from tile")

        job_title = ""
        company = ""
        job_location = ""
        link = ""
        apply_method = "Unknown"

        # ---- Title + link ----------------------------------------------------
        link_elem = None
        try:
            # New style (like in your pasted "More jobs" section)
            link_elem = job_tile.find_element(
                By.CSS_SELECTOR,
                "a.job-card-job-posting-card-wrapper__card-link",
            )
        except NoSuchElementException:
            try:
                # Older search results card
                link_elem = job_tile.find_element(
                    By.CSS_SELECTOR,
                    "a.job-card-container__link, "
                    "a.job-card-list__title",
                )
            except NoSuchElementException:
                # Last resort: any jobs/view link
                try:
                    link_elem = job_tile.find_element(
                        By.CSS_SELECTOR, "a[href*='/jobs/view/']"
                    )
                except NoSuchElementException:
                    logger.warning("Job title or link element not found in tile.")

        if link_elem is not None:
            try:
                href = link_elem.get_attribute("href") or ""
                link = href.split("?", 1)[0]

                # Many cards wrap the visible title into <strong> inside a <span>
                try:
                    strong = link_elem.find_element(By.TAG_NAME, "strong")
                    job_title = strong.text.strip()
                except NoSuchElementException:
                    job_title = (link_elem.text or "").strip()

                logger.debug(f"Job title/link extracted: {job_title} -> {link}")
            except Exception as e:
                logger.warning(f"Error extracting title/link from tile: {e}")

        # ---- Company ---------------------------------------------------------
        try:
            subtitle = job_tile.find_element(
                By.CSS_SELECTOR, ".artdeco-entity-lockup__subtitle"
            )
            company_text = (subtitle.text or "").strip()
            # Sometimes this has multiple lines; first line is usually the company
            company = company_text.split("\n")[0].strip()
        except NoSuchElementException:
            logger.warning("Company name not found in tile.")

        # ---- Location --------------------------------------------------------
        try:
            # Newer layout: caption div
            caption = job_tile.find_element(
                By.CSS_SELECTOR, ".artdeco-entity-lockup__caption"
            )
            job_location = (caption.text or "").strip()
        except NoSuchElementException:
            # Fallback: older metadata wrapper
            try:
                metadata_ul = job_tile.find_element(
                    By.CSS_SELECTOR,
                    "ul.job-card-container__metadata-wrapper, "
                    "ul.job-card-list__meta",
                )
                first_li_span = metadata_ul.find_element(By.CSS_SELECTOR, "li span")
                job_location = (first_li_span.text or "").strip()
            except NoSuchElementException:
                logger.warning("Job location not found in tile.")

        # ---- Apply method detection (Easy Apply / Applied / etc.) -----------
        try:
            footer_items = job_tile.find_elements(
                By.CSS_SELECTOR,
                "ul.job-card-list__footer-wrapper li, "
                "ul.job-card-job-posting-card-wrapper__footer-items li",
            )
            for item in footer_items:
                text = (item.text or "").strip().lower()
                if not text:
                    continue
                if "easy apply" in text:
                    apply_method = "Easy Apply"
                    break
                if "applied" in text:
                    apply_method = "Applied"
                elif "apply" in text and apply_method == "Unknown":
                    # Only set to generic "Apply" if we haven't already
                    apply_method = "Apply"
        except Exception as e:
            logger.warning(f"Job footer not parsed for apply method detection: {e}")

        logger.debug(
            f"Extracted from tile -> title='{job_title}', company='{company}', "
            f"location='{job_location}', apply_method='{apply_method}'"
        )

        return job_title, company, job_location, link, apply_method

    # -------------------------------------------------------------------------
    # Blacklist & dedupe helpers
    # -------------------------------------------------------------------------

    def is_blacklisted(self, job_title, company, link):
        logger.debug(f"Checking if job is blacklisted: {job_title} at {company}")
        job_title_words = (job_title or "").lower().split()

        title_blacklisted = any(
            word.lower() in job_title_words for word in self.title_blacklist
        )

        company_blacklisted = False
        if company:
            company_lower = company.strip().lower()
            company_blacklisted = any(
                company_lower == (c or "").strip().lower()
                for c in self.company_blacklist
            )

        link_seen = bool(link) and (link in self.seen_jobs)

        is_blacklisted = title_blacklisted or company_blacklisted or link_seen
        logger.debug(f"Job blacklisted status: {is_blacklisted}")
        return is_blacklisted

    def is_already_applied_to_job(self, job_title, company, link):
        link_seen = bool(link) and (link in self.seen_jobs)
        if link_seen:
            logger.debug(
                f"Already applied to job: {job_title} at {company}, skipping..."
            )
        return link_seen

    def is_already_applied_to_company(self, company):
        if not self.apply_once_at_company or not company:
            return False

        company_lower = company.strip().lower()
        output_files = ["success.json"]

        for file_name in output_files:
            file_path = self.output_file_directory / file_name
            if not file_path.exists():
                continue

            with open(file_path, "r", encoding="utf-8") as f:
                try:
                    existing_data = json.load(f)
                except json.JSONDecodeError:
                    continue

            for applied_job in existing_data:
                applied_company = (applied_job.get("company") or "").strip().lower()
                if applied_company == company_lower:
                    logger.debug(
                        f"Already applied at {company} "
                        "(once-per-company policy), skipping..."
                    )
                    return True

        return False
