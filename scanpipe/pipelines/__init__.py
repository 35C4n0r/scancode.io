# SPDX-License-Identifier: Apache-2.0
#
# http://nexb.com and https://github.com/nexB/scancode.io
# The ScanCode.io software is licensed under the Apache License version 2.0.
# Data generated with ScanCode.io is provided as-is without warranties.
# ScanCode is a trademark of nexB Inc.
#
# You may not use this software except in compliance with the License.
# You may obtain a copy of the License at: http://apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
# Data Generated with ScanCode.io is provided on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, either express or implied. No content created from
# ScanCode.io should be considered or used as legal advice. Consult an Attorney
# for any legal advice.
#
# ScanCode.io is a free software code scanning tool from nexB Inc. and others.
# Visit https://github.com/nexB/scancode.io for support and download.

import inspect
import logging
import traceback
from contextlib import contextmanager
from functools import wraps
from pydoc import getdoc
from pydoc import splitdoc
from timeit import default_timer as timer

from django.utils import timezone

from pyinstrument import Profiler

from scanpipe import humanize_time

logger = logging.getLogger(__name__)


class InputFileError(Exception):
    """InputFile is missing or cannot be downloaded."""


def group(*groups):
    """Mark a function as part of a particular group."""

    def decorator(obj):
        if hasattr(obj, "groups"):
            obj.groups = obj.groups.union(groups)
        else:
            setattr(obj, "groups", set(groups))
        return obj

    return decorator


class BasePipeline:
    """Base class for all pipelines."""

    # Flag specifying whether to download missing inputs as an initial step.
    download_inputs = True
    # Flag indicating if the Pipeline is an add-on, meaning it cannot be run first.
    is_addon = False

    def __init__(self, run):
        """Load the Run and Project instances."""
        self.run = run
        self.project = run.project
        self.pipeline_name = run.pipeline_name
        self.env = self.project.get_env()

    @classmethod
    def steps(cls):
        raise NotImplementedError

    @classmethod
    def get_steps(cls, groups=None):
        """
        Return the list of steps defined in the ``steps`` class method.

        If the optional ``groups`` parameter is provided, only include steps labeled
        with groups that intersect with the provided list. If a step has no groups or
        if ``groups`` is not specified, include the step in the result.
        """
        if not callable(cls.steps):
            raise TypeError("Use a ``steps(cls)`` classmethod to declare the steps.")

        steps = cls.steps()

        if groups is not None:
            steps = tuple(
                step
                for step in steps
                if not getattr(step, "groups", [])
                or set(getattr(step, "groups")).intersection(groups)
            )

        return steps

    @classmethod
    def get_doc(cls):
        """Get the doc string of this pipeline."""
        return getdoc(cls)

    @classmethod
    def get_graph(cls):
        """Return a graph of steps."""
        return [
            {
                "name": step.__name__,
                "doc": getdoc(step),
                "groups": getattr(step, "groups", []),
            }
            for step in cls.get_steps()
        ]

    @classmethod
    def get_info(cls):
        """Get a dictionary of combined information data about this pipeline."""
        summary, description = splitdoc(cls.get_doc())
        return {
            "summary": summary,
            "description": description,
            "steps": cls.get_graph(),
            "available_groups": cls.get_available_groups(),
        }

    @classmethod
    def get_summary(cls):
        """Get the doc string summary."""
        return cls.get_info()["summary"]

    @classmethod
    def get_available_groups(cls):
        return sorted(
            set(
                group_name
                for step in cls.get_steps()
                for group_name in getattr(step, "groups", [])
            )
        )

    def log(self, message):
        """Log the given `message` to the current module logger and Run instance."""
        now_as_localtime = timezone.localtime(timezone.now())
        timestamp = now_as_localtime.strftime("%Y-%m-%d %H:%M:%S.%f")[:-4]
        message = f"{timestamp} {message}"
        logger.info(message)
        self.run.append_to_log(message)

    def execute(self):
        """Execute each steps in the order defined on this pipeline class."""
        self.log(f"Pipeline [{self.pipeline_name}] starting")

        steps = self.get_steps(groups=self.run.selected_groups)

        if self.download_inputs:
            steps = (self.__class__.download_missing_inputs,) + steps

        steps_count = len(steps)
        pipeline_start_time = timer()

        for current_index, step in enumerate(steps, start=1):
            step_name = step.__name__

            self.run.set_current_step(f"{current_index}/{steps_count} {step_name}")
            self.log(f"Step [{step_name}] starting")
            step_start_time = timer()

            try:
                step(self)
            except Exception as e:
                self.log("Pipeline failed")
                tb = "".join(traceback.format_tb(e.__traceback__))
                return 1, f"{e}\n\nTraceback:\n{tb}"

            step_run_time = timer() - step_start_time
            self.log(f"Step [{step_name}] completed in {humanize_time(step_run_time)}")

        self.run.set_current_step("")  # Reset the `current_step` field on completion
        pipeline_run_time = timer() - pipeline_start_time
        self.log(f"Pipeline completed in {humanize_time(pipeline_run_time)}")

        return 0, ""

    def download_missing_inputs(self):
        """
        Download any InputSource missing on disk.
        Raise an error if any of the uploaded files is not available.
        """
        errors = []

        for input_source in self.project.inputsources.all():
            if input_source.exists():
                continue

            if input_source.is_uploaded:
                msg = f"Uploaded file {input_source} not available."
                self.log(msg)
                errors.append(msg)
                continue

            self.log(f"Fetching input from {input_source.download_url}")
            try:
                input_source.fetch()
            except Exception as error:
                self.log(f"{input_source.download_url} could not be fetched.")
                errors.append(error)

        if errors:
            raise InputFileError(errors)

    def add_error(self, exception):
        """Create a ``ProjectMessage`` ERROR record on the current `project`."""
        self.project.add_error(model=self.pipeline_name, exception=exception)

    @contextmanager
    def save_errors(self, *exceptions):
        """
        Context manager to save specified exceptions as ``ProjectMessage`` in the
        database.

        Example in a Pipeline step:

        with self.save_errors(rootfs.DistroNotFound):
            rootfs.scan_rootfs_for_system_packages(self.project, rfs)
        """
        try:
            yield
        except exceptions as error:
            self.add_error(exception=error)


class Pipeline(BasePipeline):
    """Main class for all pipelines including common step methods."""

    def flag_empty_files(self):
        """Flag empty files."""
        from scanpipe.pipes import flag

        flag.flag_empty_files(self.project)

    def flag_ignored_resources(self):
        """Flag ignored resources based on Project ``ignored_patterns`` setting."""
        from scanpipe.pipes import flag

        if ignored_patterns := self.env.get("ignored_patterns"):
            flag.flag_ignored_patterns(self.project, patterns=ignored_patterns)

    def extract_archives(self):
        """Extract archives located in the codebase/ directory with extractcode."""
        from scanpipe.pipes import scancode

        extract_errors = scancode.extract_archives(
            location=self.project.codebase_path,
            recurse=self.env.get("extract_recursively", True),
        )

        if extract_errors:
            self.add_error("\n".join(extract_errors))


def is_pipeline(obj):
    """
    Return True if the `obj` is a subclass of `Pipeline` except for the
    `Pipeline` class itself.
    """
    return inspect.isclass(obj) and issubclass(obj, Pipeline) and obj is not Pipeline


def profile(step):
    """
    Profile a Pipeline step and save the results as HTML file in the project output
    directory.

    Usage:
        @profile
        def step(self):
            pass
    """

    @wraps(step)
    def wrapper(*arg, **kwargs):
        pipeline_instance = arg[0]
        project = pipeline_instance.project

        with Profiler() as profiler:
            result = step(*arg, **kwargs)

        output_file = project.get_output_file_path("profile", "html")
        output_file.write_text(profiler.output_html())

        pipeline_instance.log(f"Profiling results at {output_file.resolve()}")

        return result

    return wrapper
