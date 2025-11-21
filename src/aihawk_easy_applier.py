import base64
import json
import os
import random
import re
import time
import traceback
from pathlib import Path
from typing import List, Optional, Any, Tuple

from httpx import HTTPStatusError
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

import src.utils as utils
from loguru import logger


class AIHawkEasyApplier:
    """
    Handles the full LinkedIn Easy Apply flow:
      - Opens the job, clicks Easy Apply
      - Walks through each Easy Apply step
      - Fills forms (old & new LinkedIn / Greenhouse style)
      - Uploads resume / cover letter
      - Submits application
    """

    def __init__(
        self,
        driver: Any,
        resume_dir: Optional[str],
        set_old_answers: List[Tuple[str, str, str]],
        gpt_answerer: Any,
        resume_generator_manager: Any,
    ):
        logger.debug("Initializing AIHawkEasyApplier")

        self.driver = driver
        self.resume_generator_manager = resume_generator_manager
        self.gpt_answerer = gpt_answerer
        self.set_old_answers = set_old_answers

        # Normalise resume path to a Path object or None
        if resume_dir is not None:
            resume_path = Path(resume_dir)
            if resume_path.exists() and resume_path.is_file():
                self.resume_path: Optional[Path] = resume_path
            else:
                logger.warning(
                    f"Provided resume path does not exist or is not a file: {resume_dir}. "
                    "Will generate a new resume if needed."
                )
                self.resume_path = None
        else:
            self.resume_path = None

        self.answers_file = "answers.json"
        self.all_data = self._load_questions_from_json()

        logger.debug("AIHawkEasyApplier initialized successfully")

    # -------------------------------------------------------------------------
    # JSON persistence for known answers
    # -------------------------------------------------------------------------

    def _load_questions_from_json(self) -> List[dict]:
        logger.debug(f"Loading cached answers from JSON file: {self.answers_file}")
        if not os.path.exists(self.answers_file):
            logger.warning("Answers JSON file not found, starting with empty cache")
            return []

        try:
            with open(self.answers_file, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                    if not isinstance(data, list):
                        raise ValueError("answers.json must contain a list of question objects")
                    logger.debug(f"Loaded {len(data)} cached answers")
                    return data
                except json.JSONDecodeError:
                    logger.error("JSON decoding failed for answers.json; resetting file on next save")
                    return []
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Error loading answers.json: {tb_str}")
            return []

    def _save_questions_to_json(self, question_data: dict) -> None:
        """
        Append a question/answer pair to answers.json.
        If the file is corrupt, it will be overwritten with a fresh list.
        """
        output_file = self.answers_file
        question_data["question"] = self._sanitize_text(question_data.get("question", ""))

        logger.debug(f"Saving question data to JSON: {question_data}")

        try:
            data: list
            try:
                if os.path.exists(output_file):
                    with open(output_file, "r", encoding="utf-8") as f:
                        try:
                            data = json.load(f)
                            if not isinstance(data, list):
                                raise ValueError(
                                    "answers.json must contain a list of question objects"
                                )
                        except json.JSONDecodeError:
                            logger.error("JSON decoding failed while reading answers.json, resetting file")
                            data = []
                else:
                    logger.warning("answers.json not found, creating a new one")
                    data = []
            except FileNotFoundError:
                logger.warning("answers.json not found on disk, will create a new file")
                data = []

            data.append(question_data)

            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)

            logger.debug("Question data saved successfully to JSON")
            # Keep in-memory cache in sync
            self.all_data = data
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Error saving questions data to JSON file: {tb_str}")
            raise Exception(f"Error saving questions data to JSON file: \nTraceback:\n{tb_str}")

    # -------------------------------------------------------------------------
    # Premium redirect handling
    # -------------------------------------------------------------------------

    def check_for_premium_redirect(self, job: Any, max_attempts: int = 3) -> None:
        current_url = self.driver.current_url
        attempts = 0

        while "linkedin.com/premium" in current_url and attempts < max_attempts:
            logger.warning("Redirected to LinkedIn Premium page. Attempting to return to job page.")
            attempts += 1
            self.driver.get(job.link)
            time.sleep(2)
            current_url = self.driver.current_url

        if "linkedin.com/premium" in current_url:
            logger.error(
                f"Failed to return to job page after {max_attempts} attempts. Cannot apply for the job."
            )
            raise Exception(
                "Redirected to LinkedIn Premium page and failed to return. Job application aborted."
            )

    # -------------------------------------------------------------------------
    # Public entrypoint
    # -------------------------------------------------------------------------

    def apply_to_job(self, job: Any) -> None:
        logger.debug(f"Applying to job via wrapper: {job}")
        try:
            self.job_apply(job)
            logger.info(f"Successfully applied to job: {job.title}")
        except Exception as e:
            logger.error(f"Failed to apply to job: {job.title}, error: {str(e)}")
            raise

    # -------------------------------------------------------------------------
    # High-level Easy Apply flow
    # -------------------------------------------------------------------------

    def job_apply(self, job: Any) -> None:
        logger.debug(f"Starting job application for job: {job}")

        try:
            # 1) Open job page
            try:
                self.driver.get(job.link)
                logger.debug(f"Navigated to job link: {job.link}")
            except Exception as e:
                logger.error(f"Failed to navigate to job link: {job.link}, error: {e}")
                raise

            time.sleep(random.uniform(3, 5))
            self.check_for_premium_redirect(job)

            # Defocus any element that might block clicking
            try:
                self.driver.execute_script(
                    "if (document.activeElement && document.activeElement.blur) { document.activeElement.blur(); }"
                )
                logger.debug("Focus removed from active element")
            except Exception as e:
                logger.debug(f"Failed to blur active element (non-fatal): {e}")

            self.check_for_premium_redirect(job)

            # 2) Find Easy Apply button
            easy_apply_button = self._find_easy_apply_button(job)
            self.check_for_premium_redirect(job)

            # 3) Capture job description
            logger.debug("Retrieving job description")
            try:
                job_description = self._get_job_description()
            except Exception as e:
                logger.warning(f"Could not retrieve job description (continuing anyway): {e}")
                job_description = ""
            job.set_job_description(job_description)

            # 4) Capture recruiter profile (if available)
            logger.debug("Retrieving recruiter link")
            recruiter_link = self._get_job_recruiter()
            job.set_recruiter_link(recruiter_link)
            logger.debug(f"Recruiter link set: {recruiter_link}")

            # 5) Click Easy Apply
            logger.debug("Clicking 'Easy Apply' button")
            actions = ActionChains(self.driver)
            actions.move_to_element(easy_apply_button).click().perform()
            logger.debug("'Easy Apply' button clicked successfully")

            # 6) Provide job context to GPT component
            logger.debug("Passing job information to GPT answerer")
            self.gpt_answerer.set_job(job)

            # 7) Run the Easy Apply wizard
            logger.debug("Filling out application form via Easy Apply wizard")
            self._fill_application_form(job)
            logger.debug(f"Job application process completed successfully for job: {job}")

        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Failed to apply to job: {job}, error: {tb_str}")
            logger.debug("Discarding application due to failure")
            self._discard_application()
            raise Exception(f"Failed to apply to job! Original exception:\nTraceback:\n{tb_str}")

    # -------------------------------------------------------------------------
    # Finding the Easy Apply button & static job info
    # -------------------------------------------------------------------------

    def _find_easy_apply_button(self, job: Any) -> WebElement:
        logger.debug("Searching for 'Easy Apply' button")
        attempt = 0

        search_methods = [
            {
                "description": "primary Easy Apply button by class",
                "find_elements": True,
                "xpath": '//button[contains(@class, "jobs-apply-button") and contains(., "Easy Apply")]',
            },
            {
                "description": "aria-label containing 'Easy Apply to'",
                "find_elements": False,
                "xpath": '//button[contains(@aria-label, "Easy Apply to")]',
            },
            {
                "description": "button text search ('Easy Apply' or 'Apply now')",
                "find_elements": False,
                "xpath": '//button[contains(normalize-space(.), "Easy Apply") or contains(normalize-space(.), "Apply now")]',
            },
        ]

        while attempt < 2:
            self.check_for_premium_redirect(job)
            self._scroll_page()

            for method in search_methods:
                try:
                    logger.debug(f"Attempting search using: {method['description']}")

                    if method.get("find_elements"):
                        buttons = self.driver.find_elements(By.XPATH, method["xpath"])
                        if buttons:
                            for index, button in enumerate(buttons):
                                try:
                                    WebDriverWait(self.driver, 10).until(EC.visibility_of(button))
                                    WebDriverWait(self.driver, 10).until(
                                        EC.element_to_be_clickable(button)
                                    )
                                    logger.debug(
                                        f"Found 'Easy Apply' button candidate #{index + 1}, "
                                        "using it for application"
                                    )
                                    return button
                                except Exception as e:
                                    logger.warning(
                                        f"Button #{index + 1} found but not clickable: {e}"
                                    )
                        else:
                            logger.debug("No Easy Apply buttons found with this strategy")
                    else:
                        button = WebDriverWait(self.driver, 10).until(
                            EC.presence_of_element_located((By.XPATH, method["xpath"]))
                        )
                        WebDriverWait(self.driver, 10).until(EC.visibility_of(button))
                        WebDriverWait(self.driver, 10).until(EC.element_to_be_clickable(button))
                        logger.debug("Found 'Easy Apply' button via single locator")
                        return button

                except TimeoutException:
                    logger.warning(f"Timeout during search using: {method['description']}")
                except Exception as e:
                    logger.warning(
                        f"Failed to locate 'Easy Apply' button using {method['description']} "
                        f"on attempt {attempt + 1}: {e}"
                    )

            self.check_for_premium_redirect(job)

            if attempt == 0:
                logger.debug("Refreshing page to retry finding 'Easy Apply' button")
                self.driver.refresh()
                time.sleep(random.randint(3, 5))

            attempt += 1

        page_source = self.driver.page_source
        logger.error("No clickable 'Easy Apply' button found after 2 attempts.")
        logger.debug(f"Page source snapshot for debugging:\n{page_source[:4000]}")
        raise Exception("No clickable 'Easy Apply' button found")

    def _get_job_description(self) -> str:
        """
        Grab the full job description text from the LinkedIn job page.
        """
        logger.debug("Getting job description")
        wait = WebDriverWait(self.driver, 15)

        try:
            container = wait.until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "article.jobs-description__container")
                )
            )
            html_content = container.find_element(
                By.CSS_SELECTOR, "div.jobs-box__html-content#job-details"
            )
            description = html_content.text.strip()
            logger.debug(f"Job description length: {len(description)} characters")
            return description
        except Exception as e:
            logger.warning(f"Primary job description structure not found: {e}")

        # Fallback – any jobs-box__html-content
        try:
            html_content = self.driver.find_element(
                By.CSS_SELECTOR, "div.jobs-box__html-content"
            )
            description = html_content.text.strip()
            logger.debug(
                "Fallback job description grabbed "
                f"({len(description)} characters)"
            )
            return description
        except Exception as e:
            logger.error(f"Job description not found in any known structure: {e}")
            raise

    def _get_job_recruiter(self) -> str:
        logger.debug("Getting job recruiter information")
        try:
            hiring_team_section = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located(
                    (By.XPATH, '//h2[normalize-space(.)="Meet the hiring team"]')
                )
            )
            logger.debug("Hiring team section found")

            recruiter_elements = hiring_team_section.find_elements(
                By.XPATH, './/following::a[contains(@href, "linkedin.com/in/")]'
            )

            if recruiter_elements:
                recruiter_element = recruiter_elements[0]
                recruiter_link = recruiter_element.get_attribute("href")
                logger.debug(f"Job recruiter link retrieved successfully: {recruiter_link}")
                return recruiter_link

            logger.debug("No recruiter link found in the hiring team section")
            return ""
        except Exception as e:
            logger.warning(f"Failed to retrieve recruiter information: {e}")
            return ""

    def _scroll_page(self) -> None:
        logger.debug("Scrolling the page")
        scrollable_element = self.driver.find_element(By.TAG_NAME, "html")
        utils.scroll_slow(self.driver, scrollable_element, step=300, reverse=False)
        utils.scroll_slow(self.driver, scrollable_element, step=300, reverse=True)

    # -------------------------------------------------------------------------
    # Easy Apply wizard: steps & modal / form handling
    # -------------------------------------------------------------------------

    def _get_easy_apply_modal(self) -> WebElement:
        """
        Locate the Easy Apply modal container.
        """
        wait = WebDriverWait(self.driver, 20)
        modal = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "div.jobs-easy-apply-modal")
            )
        )
        return modal

    def _find_form_in_modal(self, modal: WebElement) -> Optional[WebElement]:
        """
        Find the <form> element inside the Easy Apply modal, if present.

        IMPORTANT:
        - On some steps (e.g. "Review your application") there is NO <form>.
          In that case we MUST NOT raise, just return None.
        """
        try:
            # Primary: content wrapper for Easy Apply
            contents = modal.find_elements(
                By.CSS_SELECTOR, "div.jobs-easy-apply-modal__content"
            )
            for content in contents:
                forms = content.find_elements(By.TAG_NAME, "form")
                if forms:
                    logger.debug("Found <form> element inside jobs-easy-apply-modal__content")
                    return forms[0]

            # Fallback: any form within the modal
            forms = modal.find_elements(By.TAG_NAME, "form")
            if forms:
                logger.debug("Found <form> element directly under Easy Apply modal")
                return forms[0]

            logger.debug(
                "No <form> element found in Easy Apply modal "
                "(likely 'Review your application' step)"
            )
            return None
        except Exception as e:
            logger.warning(f"Error while searching for <form> in Easy Apply modal: {e}")
            return None

    def _fill_application_form(self, job: Any) -> None:
        """
        Drive the Easy Apply multi-step form until submission.

        For each step:
          - If a <form> exists, fill the step’s fields.
          - Regardless, click the primary 'Next' / 'Review' / 'Submit application' button.
          - Stop once 'Submit application' is clicked.
        """
        logger.debug(f"Filling out application form for job: {job}")

        # Wait for the Easy Apply modal to appear
        self._get_easy_apply_modal()

        max_steps = 10
        for step in range(1, max_steps + 1):
            logger.debug(f"Processing Easy Apply step {step}/{max_steps}")

            modal = self._get_easy_apply_modal()
            form = self._find_form_in_modal(modal)

            if form is not None:
                logger.debug("Form detected on this step; filling it")
                self.fill_up(form, job)
            else:
                logger.debug(
                    "No form detected on this step. "
                    "Assuming review / confirmation screen and skipping field filling."
                )

            submitted = self._next_or_submit(modal)
            if submitted:
                logger.debug("Application submitted successfully via Easy Apply wizard")
                return

        raise Exception("Easy Apply wizard did not finish within the expected number of steps")

    def _next_or_submit(self, modal: WebElement) -> bool:
        """
        Click the primary button in the Easy Apply footer.
        Returns True if this was the final 'Submit application' step.
        """
        logger.debug("Clicking 'Next' / 'Review' / 'Submit application' button")
        try:
            # Restrict search to the modal so we don't catch random LinkedIn buttons on the page
            primary_buttons = modal.find_elements(
                By.CSS_SELECTOR, "button.artdeco-button--primary"
            )
            if not primary_buttons:
                logger.error("No primary button found in Easy Apply modal footer")
                raise Exception("No primary button found in Easy Apply modal")

            next_button = primary_buttons[-1]  # right-most button is usually Next/Submit
            button_text = (next_button.text or "").strip().lower()
            logger.debug(f"Primary button label detected: '{button_text}'")

            if "submit application" in button_text or button_text == "submit":
                logger.debug("Detected final 'Submit application' button")
                self._unfollow_company(modal)
                time.sleep(random.uniform(1.5, 2.5))
                next_button.click()
                time.sleep(random.uniform(1.5, 2.5))
                return True

            # Next / Review step
            time.sleep(random.uniform(1.5, 2.5))
            next_button.click()
            time.sleep(random.uniform(3.0, 5.0))
            self._check_for_errors(modal)
            return False

        except Exception as e:
            logger.error(f"Failed to click 'Next' / 'Submit application' button: {e}")
            raise

    def _unfollow_company(self, modal: Optional[WebElement] = None) -> None:
        """
        Uncheck the 'Follow <Company>' checkbox if present in the Easy Apply footer.
        """
        scope = modal if modal is not None else self.driver
        try:
            logger.debug("Attempting to unfollow company (if follow checkbox is present)")

            # Preferred: explicit follow-company-checkbox
            checkbox_inputs = scope.find_elements(
                By.CSS_SELECTOR, "input#follow-company-checkbox"
            )
            if checkbox_inputs:
                input_el = checkbox_inputs[0]
                try:
                    if input_el.is_selected():
                        label = scope.find_element(
                            By.CSS_SELECTOR, "label[for='follow-company-checkbox']"
                        )
                        label.click()
                        logger.debug("Unfollowed company via follow-company-checkbox label")
                except Exception as e:
                    logger.debug(f"Could not inspect/click follow-company-checkbox (non-fatal): {e}")
                return

            # Fallback: a label containing the follow sentence
            labels = scope.find_elements(
                By.XPATH,
                ".//label[contains(., 'Follow') and contains(., 'stay up to date')]",
            )
            if labels:
                labels[0].click()
                logger.debug("Unfollowed company via generic follow label")
        except Exception as e:
            logger.debug(f"No follow-company checkbox to unfollow or failed to click it: {e}")

    def _check_for_errors(self, modal: Optional[WebElement] = None) -> None:
        """
        Check for inline error messages after clicking Next/Submit.
        """
        logger.debug("Checking for form errors")
        scope = modal if modal is not None else self.driver
        error_elements = scope.find_elements(By.CLASS_NAME, "artdeco-inline-feedback--error")
        if error_elements:
            messages = [e.text for e in error_elements if e.text.strip()]
            logger.error(f"Form submission failed with errors: {messages}")
            raise Exception(f"Failed answering or file upload. {messages}")

    def _discard_application(self) -> None:
        """
        Close the Easy Apply modal if it's open.
        """
        logger.debug("Discarding application (closing Easy Apply modal)")
        try:
            # 1) Most reliable: data-test-modal-close-btn
            close_btns = self.driver.find_elements(
                By.CSS_SELECTOR, "button[data-test-modal-close-btn]"
            )
            # 2) Fallback: generic modal dismiss button
            if not close_btns:
                close_btns = self.driver.find_elements(
                    By.CSS_SELECTOR, "button.artdeco-modal__dismiss"
                )
            # 3) Last fallback: any button with aria-label='Dismiss' inside modal
            if not close_btns:
                close_btns = self.driver.find_elements(
                    By.CSS_SELECTOR, "div.artdeco-modal button[aria-label='Dismiss']"
                )

            if close_btns:
                close_btns[0].click()
                logger.debug("Easy Apply modal dismissed")
            else:
                logger.warning("Could not find Easy Apply modal dismiss button")
        except Exception as e:
            logger.warning(f"Failed to discard application (non-fatal): {e}")

    # -------------------------------------------------------------------------
    # Per-step form filling
    # -------------------------------------------------------------------------

    def fill_up(self, form: WebElement, job: Any) -> None:
        """
        Fill all fields in a single Easy Apply form step.

        Supports:
          - Upload fields (resume / cover letter)
          - Old-style LinkedIn sections (.jobs-easy-apply-form-section__grouping)
          - New Greenhouse-style elements (div[data-test-form-element], fieldset etc.)
        """
        logger.debug(f"Filling up form sections for job: {job}")

        try:
            # 1) Handle file uploads (resume / cover letter) if present in this step
            self._handle_upload_fields(form, job)

            # 2) Process groups of questions
            #    a) Legacy LinkedIn grouping
            sections = form.find_elements(
                By.CLASS_NAME, "jobs-easy-apply-form-section__grouping"
            )
            #    b) New-style Greenhouse groupings
            sections.extend(form.find_elements(By.CSS_SELECTOR, "div[data-test-form-element]"))

            for section in sections:
                self._process_form_section(section)

        except Exception as e:
            logger.error(f"Failed to process form step: {e}")

    # -------------------------------------------------------------------------
    # Section / question handling
    # -------------------------------------------------------------------------

    def _process_form_section(self, section: WebElement) -> None:
        logger.debug("Processing form section")

        if self._handle_terms_of_service(section):
            logger.debug("Handled terms of service section")
            return

        if self._find_and_handle_radio_question(section):
            logger.debug("Handled radio question section")
            return

        if self._find_and_handle_textbox_question(section):
            logger.debug("Handled textbox question section")
            return

        if self._find_and_handle_date_question(section):
            logger.debug("Handled date question section")
            return

        if self._find_and_handle_dropdown_question(section):
            logger.debug("Handled dropdown question section")
            return

    def _handle_terms_of_service(self, element: WebElement) -> bool:
        checkbox_labels = element.find_elements(By.TAG_NAME, "label")
        if checkbox_labels:
            text = checkbox_labels[0].text.lower()
            if any(
                term in text
                for term in ["terms of service", "privacy policy", "terms of use"]
            ):
                try:
                    checkbox_labels[0].click()
                    logger.debug("Clicked terms of service / privacy policy checkbox")
                    return True
                except Exception as e:
                    logger.debug(f"Failed to click terms of service checkbox (non-fatal): {e}")
        return False

    def _find_and_handle_radio_question(self, section: WebElement) -> bool:
        """
        Handle both:
          - New LinkedIn/Greenhouse-style: fieldset[data-test-form-builder-radio-button-form-component]
          - Older style: .jobs-easy-apply-form-element with .fb-text-selectable__option
        """
        try:
            # New-style radio groups first
            fieldsets = section.find_elements(
                By.CSS_SELECTOR,
                "fieldset[data-test-form-builder-radio-button-form-component='true'], "
                "fieldset[data-test-form-builder-radio-button-form-component='true']",
            )

            radios: List[WebElement] = []
            question_text = ""

            if fieldsets:
                fieldset = fieldsets[0]
                try:
                    legend = fieldset.find_element(By.TAG_NAME, "legend")
                    question_text = legend.text.lower().strip()
                except NoSuchElementException:
                    question_text = section.text.lower().strip()

                option_wrappers = fieldset.find_elements(
                    By.CSS_SELECTOR, "[data-test-text-selectable-option]"
                )
                for wrapper in option_wrappers:
                    try:
                        label_el = wrapper.find_element(By.TAG_NAME, "label")
                        if label_el.text.strip():
                            radios.append(label_el)
                    except NoSuchElementException:
                        continue

                # Fallback: all labels inside fieldset (except legend)
                if not radios:
                    labels = fieldset.find_elements(By.TAG_NAME, "label")
                    radios.extend(labels)

            else:
                # Old-style structure
                try:
                    question_element = section.find_element(
                        By.CLASS_NAME, "jobs-easy-apply-form-element"
                    )
                    radios = question_element.find_elements(
                        By.CLASS_NAME, "fb-text-selectable__option"
                    )
                    question_text = section.text.lower().strip()
                except NoSuchElementException:
                    radios = []

            if not radios:
                return False

            options = [r.text.lower().strip() for r in radios if r.text.strip()]
            if not options:
                return False

            sanitized_q = self._sanitize_text(question_text)
            existing_answer: Optional[str] = None
            for item in self.all_data:
                if item.get("type") == "radio" and item.get("question") == sanitized_q:
                    existing_answer = item.get("answer")
                    break

            if existing_answer:
                answer = existing_answer
                logger.debug(
                    f"Using cached radio answer '{answer}' for question '{question_text}'"
                )
            else:
                logger.debug(
                    f"No cached radio answer for '{question_text}', querying model from options {options}"
                )
                answer = self.gpt_answerer.answer_question_from_options(
                    question_text, options
                )
                self._save_questions_to_json(
                    {"type": "radio", "question": question_text, "answer": answer}
                )

            self._select_radio(radios, answer)
            return True

        except Exception as e:
            logger.warning(f"Failed to handle radio question: {e}", exc_info=True)
            return False

    def _find_and_handle_textbox_question(self, section: WebElement) -> bool:
        logger.debug("Searching for text fields in the section")

        # Any input/textarea inside this section
        text_fields = section.find_elements(By.TAG_NAME, "input") + section.find_elements(
            By.TAG_NAME, "textarea"
        )
        if not text_fields:
            logger.debug("No text fields found in the section")
            return False

        text_field = text_fields[0]

        try:
            label_el = section.find_element(By.TAG_NAME, "label")
            question_text_raw = label_el.text
        except NoSuchElementException:
            question_text_raw = section.text

        question_text = (question_text_raw or "").lower().strip()
        logger.debug(f"Found text field with label: {question_text}")

        is_numeric = self._is_numeric_field(text_field)
        question_type = "numeric" if is_numeric else "textbox"
        logger.debug(f"Is the field numeric? {'Yes' if is_numeric else 'No'}")

        # Handle cover letter-like questions specially
        is_cover_letter = "cover letter" in question_text

        existing_answer: Optional[str] = None
        if not is_cover_letter:
            sanitized_q = self._sanitize_text(question_text)
            for item in self.all_data:
                if (
                    item.get("type") == question_type
                    and item.get("question") == sanitized_q
                ):
                    existing_answer = item.get("answer")
                    logger.debug(f"Found existing answer: {existing_answer}")
                    break

        if existing_answer and not is_cover_letter:
            answer = existing_answer
            logger.debug(f"Using existing answer: {answer}")
        else:
            if is_numeric:
                answer = self.gpt_answerer.answer_question_numeric(question_text)
                logger.debug(f"Generated numeric answer: {answer}")
            else:
                answer = self.gpt_answerer.answer_question_textual_wide_range(
                    question_text
                )
                logger.debug(f"Generated textual answer: {answer}")

        self._enter_text(text_field, str(answer))
        logger.debug("Entered answer into the textbox")

        # Save non-cover-letter answers for reuse
        if not is_cover_letter:
            self._save_questions_to_json(
                {"type": question_type, "question": question_text, "answer": str(answer)}
            )
            logger.debug("Saved non-cover-letter answer to JSON")

        # Some fields have autocomplete dropdowns
        try:
            time.sleep(0.5)
            text_field.send_keys(Keys.ARROW_DOWN)
            text_field.send_keys(Keys.ENTER)
            logger.debug("Attempted to select first autocomplete option (if any)")
        except Exception:
            pass

        return True

    def _find_and_handle_date_question(self, section: WebElement) -> bool:
        # Datepickers: either artdeco-datepicker or a plain <input type="date">
        date_fields = section.find_elements(
            By.CSS_SELECTOR, "input.artdeco-datepicker__input, input[type='date']"
        )
        if not date_fields:
            return False

        date_field = date_fields[0]
        question_text = section.text.lower()

        existing_answer = None
        sanitized_q = self._sanitize_text(question_text)
        for item in self.all_data:
            if item.get("type") == "date" and item.get("question") == sanitized_q:
                existing_answer = item.get("answer")
                break

        if existing_answer:
            answer_text = existing_answer
            logger.debug(
                f"Using cached date answer '{answer_text}' for question '{question_text}'"
            )
        else:
            answer_date = self.gpt_answerer.answer_question_date()
            answer_text = answer_date.strftime("%Y-%m-%d")
            logger.debug(f"Generated new date answer: {answer_text}")
            self._save_questions_to_json(
                {"type": "date", "question": question_text, "answer": answer_text}
            )

        self._enter_text(date_field, answer_text)
        logger.debug("Entered date answer into date field")
        return True

    def _find_and_handle_dropdown_question(self, section: WebElement) -> bool:
        """
        Handle <select> based questions (old & new UI, including Greenhouse-style
        text-entity-list components).
        """
        try:
            # Look for any select in this section
            dropdowns = section.find_elements(By.TAG_NAME, "select")
            if not dropdowns:
                dropdowns = section.find_elements(
                    By.CSS_SELECTOR, "[data-test-text-entity-list-form-select]"
                )

            if not dropdowns:
                return False

            dropdown = dropdowns[0]
            select = Select(dropdown)
            options = [opt.text.strip() for opt in select.options if opt.text.strip()]
            logger.debug(f"Dropdown options found: {options}")

            if not options:
                return False

            # Question text from nearest label
            try:
                label_el = section.find_element(By.TAG_NAME, "label")
                question_text_raw = label_el.text
            except NoSuchElementException:
                question_text_raw = section.text

            question_text = (question_text_raw or "").lower().strip()
            logger.debug(f"Processing dropdown question: {question_text}")

            current_selection = (select.first_selected_option.text or "").strip()
            logger.debug(f"Current dropdown selection: '{current_selection}'")

            sanitized_q = self._sanitize_text(question_text)
            existing_answer: Optional[str] = None
            for item in self.all_data:
                if item.get("type") == "dropdown" and item.get("question") == sanitized_q:
                    existing_answer = item.get("answer")
                    break

            if existing_answer:
                answer = existing_answer
                logger.debug(
                    f"Found existing dropdown answer for '{question_text}': {answer}"
                )
            else:
                logger.debug(
                    f"No existing dropdown answer for '{question_text}', querying model"
                )
                answer = self.gpt_answerer.answer_question_from_options(
                    question_text, options
                )
                self._save_questions_to_json(
                    {"type": "dropdown", "question": question_text, "answer": answer}
                )

            if current_selection != answer:
                logger.debug(f"Updating selection to: {answer}")
                self._select_dropdown_option(dropdown, answer)
            return True

        except Exception as e:
            logger.warning(f"Failed to handle dropdown question: {e}", exc_info=True)
            return False

    # -------------------------------------------------------------------------
    # Upload fields (resume / cover letter)
    # -------------------------------------------------------------------------

    def _handle_upload_fields(self, scope: WebElement, job: Any) -> None:
        """
        Handle file upload inputs (resume / cover letter) that appear inside
        the current form step.
        """
        logger.debug("Handling upload fields (resume / cover letter)")

        # Try to expand "Show more resumes" if present in this step
        try:
            show_more_buttons = scope.find_elements(
                By.XPATH, ".//button[contains(@aria-label, 'Show more resumes')]"
            )
            if show_more_buttons:
                show_more_buttons[0].click()
                logger.debug("Clicked 'Show more resumes' button")
        except Exception as e:
            logger.debug(f"'Show more resumes' button not found or not clickable (non-fatal): {e}")

        file_upload_elements = scope.find_elements(By.XPATH, ".//input[@type='file']")
        if not file_upload_elements:
            logger.debug("No file upload inputs found in this step")
            return

        for input_el in file_upload_elements:
            try:
                parent = input_el.find_element(By.XPATH, "..")
            except NoSuchElementException:
                parent = input_el

            # Make sure the input is visible for send_keys
            try:
                self.driver.execute_script(
                    "arguments[0].classList.remove('hidden');", input_el
                )
            except Exception:
                pass

            parent_text = (parent.text or "").lower()
            output = self.gpt_answerer.resume_or_cover(parent_text)
            logger.debug(f"Model classified upload field as: {output}")

            if "resume" in output:
                logger.debug("Detected resume upload field")
                if self.resume_path is not None and self.resume_path.is_file():
                    logger.debug(f"Uploading existing resume: {self.resume_path}")
                    input_el.send_keys(str(self.resume_path.resolve()))
                    job.pdf_path = str(self.resume_path.resolve())
                else:
                    logger.debug(
                        "No valid resume path found, generating a new resume PDF for upload"
                    )
                    self._create_and_upload_resume(input_el, job)

            elif "cover" in output:
                logger.debug("Detected cover letter upload field")
                self._create_and_upload_cover_letter(input_el, job)

        logger.debug("Finished handling upload fields for this step")

    def _create_and_upload_resume(self, element: WebElement, job: Any) -> None:
        logger.debug("Starting the process of creating and uploading resume")
        folder_path = "generated_cv"

        try:
            os.makedirs(folder_path, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create directory '{folder_path}': {e}")
            raise

        while True:
            try:
                timestamp = int(time.time())
                file_path_pdf = os.path.join(folder_path, f"CV_{timestamp}.pdf")
                logger.debug(f"Generated file path for resume: {file_path_pdf}")

                logger.debug(f"Generating resume for job: {job.title} at {job.company}")
                resume_pdf_base64 = self.resume_generator_manager.pdf_base64(
                    job_description_text=job.description
                )
                with open(file_path_pdf, "xb") as f:
                    f.write(base64.b64decode(resume_pdf_base64))
                logger.debug(
                    f"Resume successfully generated and saved to: {file_path_pdf}"
                )
                break

            except HTTPStatusError as e:
                if e.response.status_code == 429:
                    retry_after = e.response.headers.get("retry-after")
                    retry_after_ms = e.response.headers.get("retry-after-ms")

                    if retry_after:
                        wait_time = int(retry_after)
                        logger.warning(
                            f"Rate limit exceeded, waiting {wait_time} seconds before retrying..."
                        )
                    elif retry_after_ms:
                        wait_time = int(retry_after_ms) / 1000.0
                        logger.warning(
                            f"Rate limit exceeded, waiting {wait_time} milliseconds before retrying..."
                        )
                    else:
                        wait_time = 20
                        logger.warning(
                            f"Rate limit exceeded, waiting {wait_time} seconds before retrying..."
                        )

                    time.sleep(wait_time)
                else:
                    logger.error(f"HTTP error while generating resume: {e}")
                    raise

            except Exception as e:
                tb_str = traceback.format_exc()
                logger.error(f"Failed to generate resume: {e}")
                logger.error(f"Traceback: {tb_str}")
                if "RateLimitError" in str(e):
                    logger.warning("Rate limit error encountered, retrying...")
                    time.sleep(20)
                else:
                    raise

        # Validate size and extension
        file_size = os.path.getsize(file_path_pdf)
        max_file_size = 2 * 1024 * 1024  # 2 MB
        logger.debug(f"Resume file size: {file_size} bytes")
        if file_size > max_file_size:
            logger.error(f"Resume file size exceeds 2 MB: {file_size} bytes")
            raise ValueError("Resume file size exceeds the maximum limit of 2 MB.")

        allowed_extensions = {".pdf", ".doc", ".docx"}
        file_extension = os.path.splitext(file_path_pdf)[1].lower()
        logger.debug(f"Resume file extension: {file_extension}")
        if file_extension not in allowed_extensions:
            logger.error(f"Invalid resume file format: {file_extension}")
            raise ValueError(
                "Resume file format is not allowed. Only PDF, DOC, and DOCX formats are supported."
            )

        try:
            logger.debug(f"Uploading resume from path: {file_path_pdf}")
            element.send_keys(os.path.abspath(file_path_pdf))
            job.pdf_path = os.path.abspath(file_path_pdf)
            time.sleep(2)
            logger.debug("Resume created and uploaded successfully")
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Resume upload failed: {tb_str}")
            raise Exception(f"Upload failed: \nTraceback:\n{tb_str}")

    def _create_and_upload_cover_letter(self, element: WebElement, job: Any) -> None:
        logger.debug("Starting the process of creating and uploading cover letter")

        cover_letter_text = self.gpt_answerer.answer_question_textual_wide_range(
            "Write a cover letter"
        )
        folder_path = "generated_cv"

        try:
            os.makedirs(folder_path, exist_ok=True)
        except Exception as e:
            logger.error(f"Failed to create directory '{folder_path}': {e}")
            raise

        while True:
            try:
                timestamp = int(time.time())
                file_path_pdf = os.path.join(
                    folder_path, f"Cover_Letter_{timestamp}.pdf"
                )
                logger.debug(
                    f"Generated file path for cover letter: {file_path_pdf}"
                )

                c = canvas.Canvas(file_path_pdf, pagesize=A4)
                page_width, page_height = A4
                text_object = c.beginText(50, page_height - 50)
                text_object.setFont("Helvetica", 12)

                max_width = page_width - 100
                bottom_margin = 50

                def split_text_by_width(text, font, font_size, max_width_):
                    wrapped_lines = []
                    for line in text.splitlines():
                        if stringWidth(line, font, font_size) > max_width_:
                            words = line.split()
                            new_line = ""
                            for word in words:
                                if (
                                    stringWidth(
                                        new_line + word + " ", font, font_size
                                    )
                                    <= max_width_
                                ):
                                    new_line += word + " "
                                else:
                                    wrapped_lines.append(new_line.strip())
                                    new_line = word + " "
                            wrapped_lines.append(new_line.strip())
                        else:
                            wrapped_lines.append(line)
                    return wrapped_lines

                lines = split_text_by_width(
                    cover_letter_text, "Helvetica", 12, max_width
                )

                for line in lines:
                    if text_object.getY() > bottom_margin:
                        text_object.textLine(line)
                    else:
                        c.drawText(text_object)
                        c.showPage()
                        text_object = c.beginText(50, page_height - 50)
                        text_object.setFont("Helvetica", 12)
                        text_object.textLine(line)

                c.drawText(text_object)
                c.save()
                logger.debug(
                    f"Cover letter successfully generated and saved to: {file_path_pdf}"
                )
                break

            except Exception as e:
                tb_str = traceback.format_exc()
                logger.error(f"Failed to generate cover letter: {e}")
                logger.error(f"Traceback: {tb_str}")
                raise

        # Validate size and extension
        file_size = os.path.getsize(file_path_pdf)
        max_file_size = 2 * 1024 * 1024  # 2 MB
        logger.debug(f"Cover letter file size: {file_size} bytes")
        if file_size > max_file_size:
            logger.error(
                f"Cover letter file size exceeds 2 MB: {file_size} bytes"
            )
            raise ValueError(
                "Cover letter file size exceeds the maximum limit of 2 MB."
            )

        allowed_extensions = {".pdf", ".doc", ".docx"}
        file_extension = os.path.splitext(file_path_pdf)[1].lower()
        logger.debug(f"Cover letter file extension: {file_extension}")
        if file_extension not in allowed_extensions:
            logger.error(f"Invalid cover letter file format: {file_extension}")
            raise ValueError(
                "Cover letter file format is not allowed. Only PDF, DOC, and DOCX formats are supported."
            )

        try:
            logger.debug(f"Uploading cover letter from path: {file_path_pdf}")
            element.send_keys(os.path.abspath(file_path_pdf))
            job.cover_letter_path = os.path.abspath(file_path_pdf)
            time.sleep(2)
            logger.debug("Cover letter created and uploaded successfully")
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Cover letter upload failed: {tb_str}")
            raise Exception(f"Upload failed: \nTraceback:\n{tb_str}")

    # -------------------------------------------------------------------------
    # Low-level helpers
    # -------------------------------------------------------------------------

    def _is_numeric_field(self, field: WebElement) -> bool:
        field_type = (field.get_attribute("type") or "").lower()
        field_id = (field.get_attribute("id") or "").lower()
        is_numeric = (
            "numeric" in field_id
            or field_type == "number"
            or ("text" == field_type and "numeric" in field_id)
        )
        logger.debug(
            f"Field type: '{field_type}', Field ID: '{field_id}', Is numeric: {is_numeric}"
        )
        return is_numeric

    def _enter_text(self, element: WebElement, text: str) -> None:
        logger.debug(f"Entering text into field: '{text}'")
        try:
            element.clear()
        except Exception:
            # Some inputs don't support clear(); ignore
            pass
        element.send_keys(text)

    def _select_radio(self, radio_containers: List[WebElement], answer: str) -> None:
        logger.debug(f"Selecting radio option matching answer: '{answer}'")
        answer_lower = (answer or "").lower()
        fallback = None

        for container in radio_containers:
            try:
                text = (container.text or "").lower()
                if not fallback:
                    fallback = container
                if answer_lower and answer_lower in text:
                    self._click_radio_container(container)
                    logger.debug(f"Selected radio option: '{text.strip()}'")
                    return
            except Exception:
                continue

        if fallback:
            logger.debug("Could not match answer; selecting fallback radio option")
            self._click_radio_container(fallback)

    def _click_radio_container(self, container: WebElement) -> None:
        try:
            if container.tag_name.lower() == "label":
                container.click()
                return
            label = container.find_element(By.TAG_NAME, "label")
            label.click()
        except NoSuchElementException:
            container.click()

    def _select_dropdown_option(self, element: WebElement, text: str) -> None:
        logger.debug(f"Selecting dropdown option: '{text}'")
        select = Select(element)
        # Try exact match first
        for option in select.options:
            if (option.text or "").strip().lower() == (text or "").strip().lower():
                select.select_by_visible_text(option.text)
                return
        # Fallback: try partial match
        for option in select.options:
            if (text or "").strip().lower() in (option.text or "").strip().lower():
                select.select_by_visible_text(option.text)
                return
        # Last resort: let Selenium try the original string (may raise)
        select.select_by_visible_text(text)

    def _sanitize_text(self, text: str) -> str:
        text = text or ""
        sanitized_text = text.lower().strip().replace('"', "").replace("\\", "")
        sanitized_text = re.sub(r"[\x00-\x1F\x7F]", "", sanitized_text).replace(
            "\n", " "
        ).replace("\r", "").rstrip(",")
        logger.debug(f"Sanitized text: {sanitized_text}")
        return sanitized_text
