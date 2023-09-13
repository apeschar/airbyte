#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#
import os
import sys
from itertools import product
from pathlib import Path
from typing import List, Type
from unittest.mock import MagicMock

import anyio
import dagger
from base_images import ALL_BASE_IMAGES, common, console, consts, errors, hacks, python
from py_markdown_table.markdown_table import markdown_table  # type: ignore
from rich.status import Status

DOCKERFILE_HEADER = """
# This file is generated by base_images/build.py. Please do not edit it manually.
# It is not used by Airbyte internal build process.
# It is meant for documentation and debugging purposes.
"""


def generate_dockerfile(base_image_version: common.AirbyteConnectorBaseImage):
    """
    Generates the dockerfiles for all the base images.
    """
    dockerfile = hacks.get_container_dockerfile(base_image_version.container)
    dockerfile_directory = Path(consts.PROJECT_DIR / "generated" / "dockerfiles" / base_image_version.platform)
    dockerfile_directory.mkdir(exist_ok=True, parents=True)
    dockerfile_path = Path(dockerfile_directory / f"{base_image_version.name_with_tag}.Dockerfile")
    dockerfile = DOCKERFILE_HEADER + "\n" + dockerfile + "\n"
    dockerfile_path.write_text(dockerfile)
    console.log(
        f":whale2: Generated Dockerfile for {base_image_version.name_with_tag} for {base_image_version.platform}: {dockerfile_path}",
        highlight=False,
    )


async def run_sanity_checks(base_image_version: common.AirbyteConnectorBaseImage) -> bool:
    """
    Runs sanity checks on a base images.
    Sanity checks are declared in the base image version classes by implementing the run_sanity_checks function.
    Sanity checks are commands executed on the base image container, we check the output of these command to make sure the base image is working as expected.
    """
    try:
        await base_image_version.run_sanity_checks_for_version()
        console.log(
            f":white_check_mark: Successfully ran sanity checks on {base_image_version.name_with_tag} for {base_image_version.platform}",
            highlight=False,
        )
        return True
    except errors.SanityCheckError as sanity_check_error:
        console.log(
            f":cross_mark: Sanity checks failure on {base_image_version.name_with_tag} for {base_image_version.platform}: {sanity_check_error}",
            style="bold red",
            highlight=False,
        )
        return False


def write_changelog_file(changelog_path: Path, base_image_name: str, base_images_classes: List[Type[common.AirbyteConnectorBaseImage]]):
    """Writes the changelog file locally for a given base image. Per version entries are generated from the base_images Mapping.

    Args:
        changelog_path (Path): Local absolute path to the changelog file.
        base_image_name (str): The name of the base image e.g airbyte-python-connectors-base .
        base_images_classes (List[Type[common.AirbyteConnectorBaseImage]): All the base images versions for a given base image.
    """

    entries = [
        {
            "Version": f"[{base_version_image_class.version}]({base_version_image_class.github_url})",
            "Changelog": base_version_image_class.changelog_entry,
        }
        for base_version_image_class in base_images_classes
    ]
    markdown = markdown_table(entries).set_params(row_sep="markdown", quote=False).get_markdown()
    with open(changelog_path, "w") as f:
        f.write(f"# Changelog for {base_image_name}\n\n")
        f.write(markdown)


async def a_build(current_status: Status) -> bool:
    current_status.update(":dagger: Initializing Dagger")
    if consts.DEBUG:
        dagger_config = dagger.Config(log_output=sys.stderr)
    else:
        dagger_logs_path = Path("/tmp/base_images_project_build_dagger_logs.log")
        dagger_logs_path.unlink(missing_ok=True)
        dagger_logs_path.touch()
        dagger_config = dagger.Config(log_output=open(dagger_logs_path))
        console.log(f":information_source: Dagger logs will be written to {dagger_logs_path}")
    sanity_check_successes = []
    async with dagger.Connection(dagger_config) as dagger_client:
        for platform, BaseImageVersion in product(consts.SUPPORTED_PLATFORMS, ALL_BASE_IMAGES):
            base_image_version = BaseImageVersion(dagger_client, platform)
            current_status.update(
                f":whale2: Generating dockerfile for {base_image_version.name_with_tag} for {base_image_version.platform}"
            )
            generate_dockerfile(base_image_version)
            current_status.update(
                f":mag_right: Running sanity checks on {base_image_version.name_with_tag} for {base_image_version.platform}"
            )
            success = await run_sanity_checks(base_image_version)
            sanity_check_successes.append(success)
    return all(sanity_check_successes)


def main():
    """
    This function is called by the build command, currently via poetry run build.
    It's currently meant to be run locally by developers to generate the changelog and run sanity checks.
    It can eventually be run in CI to generate the changelog and run sanity checks.

    1. Run sanity checks on all the base images.
    2. Write the changelog for the python base image.

    This function calls Dagger to run the sanity checks.
    If you don't have the base base image locally it will be pulled, which can take a while.
    Subsequent runs will be faster as the base images layers and sanity checks layers will be cached locally.
    """
    try:
        default_build_status = console.status("Building the project", spinner="bouncingBall")
        disabled_build_status = MagicMock(default_build_status)
        build_status = default_build_status if not consts.DEBUG else disabled_build_status  # type: ignore
        with build_status as current_status:
            success = anyio.run(a_build, current_status)
            python_changelog_path = Path(consts.PROJECT_DIR / "CHANGELOG_PYTHON_CONNECTOR_BASE_IMAGE.md")
            if not success:
                console.log(
                    ":bomb: Sanity checks failed. Feel free to prepend the command with LOG_LEVEL=DEBUG if you want to investigate Dagger logs.",
                    style="bold red",
                )
            else:
                # build_status.update(f"Writing the changelog to {python_changelog_path}")
                write_changelog_file(python_changelog_path, python.AirbytePythonConnectorBaseImage.image_name, python.ALL_BASE_IMAGES)
                console.log(
                    f":memo: Wrote the updated changelog to {python_changelog_path}.",
                )
                if os.getenv("GIT_HOOK"):
                    console.log("[bold green] The updated changelog and dockerfile files were commited.[/bold green]")
                else:
                    console.log("[bold green]You can now commit and push the changelog and the generated dockerfiles![/bold green]")
            if not success:
                sys.exit(1)
    except KeyboardInterrupt:
        console.log(":bomb: Aborted the build.", style="bold red")
        sys.exit(1)


if __name__ == "__main__":
    main()