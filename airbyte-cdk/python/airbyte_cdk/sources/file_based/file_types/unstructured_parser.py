#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#
import logging
import os
import traceback
from io import BytesIO, IOBase
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

import backoff
import dpath.util
import requests
from airbyte_cdk.sources.file_based.config.file_based_stream_config import FileBasedStreamConfig
from airbyte_cdk.sources.file_based.config.unstructured_format import (
    APIParameterConfigModel,
    APIProcessingConfigModel,
    LocalProcessingConfigModel,
    UnstructuredFormat,
)
from airbyte_cdk.sources.file_based.exceptions import FileBasedSourceError, RecordParseError
from airbyte_cdk.sources.file_based.file_based_stream_reader import AbstractFileBasedStreamReader, FileReadMode
from airbyte_cdk.sources.file_based.file_types.file_type_parser import FileTypeParser
from airbyte_cdk.sources.file_based.remote_file import RemoteFile
from airbyte_cdk.sources.file_based.schema_helpers import SchemaType
from unstructured.documents.elements import Formula, ListItem, Title
from unstructured.file_utils.filetype import FILETYPE_TO_MIMETYPE, STR_TO_FILETYPE, FileType, detect_filetype

unstructured_partition_pdf = None
unstructured_partition_docx = None
unstructured_partition_pptx = None


def optional_decode(contents: Union[str, bytes]) -> str:
    if isinstance(contents, bytes):
        return contents.decode("utf-8")
    return contents


def _import_unstructured() -> None:
    """Dynamically imported as needed, due to slow import speed."""
    global unstructured_partition_pdf
    global unstructured_partition_docx
    global unstructured_partition_pptx
    from unstructured.partition.docx import partition_docx
    from unstructured.partition.pdf import partition_pdf
    from unstructured.partition.pptx import partition_pptx

    # separate global variables to properly propagate typing
    unstructured_partition_pdf = partition_pdf
    unstructured_partition_docx = partition_docx
    unstructured_partition_pptx = partition_pptx


def user_error(e: Exception) -> bool:
    """
    Return True if this exception is caused by user error, False otherwise.
    """
    if not isinstance(e, requests.exceptions.RequestException):
        return False
    return bool(e.response and 400 <= e.response.status_code < 500)


CLOUD_DEPLOYMENT_MODE = "cloud"


class UnstructuredParser(FileTypeParser):
    @property
    def parser_max_n_files_for_schema_inference(self) -> Optional[int]:
        """
        Just check one file as the schema is static
        """
        return 1

    @property
    def parser_max_n_files_for_parsability(self) -> Optional[int]:
        """
        Do not check any files for parsability because it might be an expensive operation and doesn't give much confidence whether the sync will succeed.
        """
        return 0

    async def infer_schema(
        self,
        config: FileBasedStreamConfig,
        file: RemoteFile,
        stream_reader: AbstractFileBasedStreamReader,
        logger: logging.Logger,
    ) -> SchemaType:
        format = _extract_format(config)
        with stream_reader.open_file(file, self.file_read_mode, None, logger) as file_handle:
            filetype = self._get_filetype(file_handle, file)

            if filetype not in self._supported_file_types():
                self._handle_unprocessable_file(file, format.skip_unprocessable_file_types, logger)

            return {
                "content": {"type": "string"},
                "document_key": {"type": "string"},
            }

    def parse_records(
        self,
        config: FileBasedStreamConfig,
        file: RemoteFile,
        stream_reader: AbstractFileBasedStreamReader,
        logger: logging.Logger,
        discovered_schema: Optional[Mapping[str, SchemaType]],
    ) -> Iterable[Dict[str, Any]]:
        format = _extract_format(config)
        with stream_reader.open_file(file, self.file_read_mode, None, logger) as file_handle:
            markdown = self._read_file(file_handle, file, format, logger)
            if markdown is not None:
                yield {
                    "content": markdown,
                    "document_key": file.uri,
                }

    def _read_file(self, file_handle: IOBase, remote_file: RemoteFile, format: UnstructuredFormat, logger: logging.Logger) -> Optional[str]:
        filetype = self._get_filetype(file_handle, remote_file)

        if filetype == FileType.MD:
            file_content: bytes = file_handle.read()
            decoded_content: str = optional_decode(file_content)
            return decoded_content
        if filetype not in self._supported_file_types():
            self._handle_unprocessable_file(remote_file, format.skip_unprocessable_file_types, logger)
            return None
        if format.processing.mode == "local":
            return self._read_file_locally(file_handle, filetype)
        elif format.processing.mode == "api":
            return self._read_file_remotely_with_retries(file_handle, format.processing, filetype)

    def _params_to_dict(self, params: Optional[List[APIParameterConfigModel]]) -> Dict[str, Union[str, List[str]]]:
        result_dict: Dict[str, Union[str, List[str]]] = {}
        if params is None:
            return result_dict
        for item in params:
            key = item.name
            value = item.value
            if key in result_dict:
                existing_value = result_dict[key]
                # If the key already exists, append the new value to its list
                if isinstance(existing_value, list):
                    existing_value.append(value)
                else:
                    result_dict[key] = [existing_value, value]
            else:
                # If the key doesn't exist, add it to the dictionary
                result_dict[key] = value

        return result_dict

    def check_config(self, config: FileBasedStreamConfig) -> Tuple[bool, Optional[str]]:
        """
        Perform a connection check for the parser config:
        - Verify that encryption is enabled if the API is hosted on a cloud instance.
        - Verify that the API can extract text from a file.
        """
        format_config = _extract_format(config)
        if isinstance(format_config.processing, LocalProcessingConfigModel):
            return True, None

        deployment_mode = os.environ.get("DEPLOYMENT_MODE", "")
        if deployment_mode.casefold() == CLOUD_DEPLOYMENT_MODE and not format_config.processing.api_url.startswith("https://"):
            return False, "Base URL must start with https://"

        try:
            self._read_file_remotely(BytesIO(b"# Airbyte source connection test"), format_config.processing, FileType.MD)
        except:
            return False, "".join(traceback.format_exc())

        return True, None

    @backoff.on_exception(backoff.expo, requests.exceptions.RequestException, max_tries=5, giveup=user_error)
    def _read_file_remotely_with_retries(self, file_handle: IOBase, format: APIProcessingConfigModel, filetype: FileType) -> Optional[str]:
        """
        Read a file remotely, retrying up to 5 times if the error is not caused by user error. This is useful for transient network errors or the API server being overloaded temporarily.
        """
        return self._read_file_remotely(file_handle, format, filetype)

    def _read_file_remotely(self, file_handle: IOBase, format: APIProcessingConfigModel, filetype: FileType) -> Optional[str]:
        headers = {"accept": "application/json", "unstructured-api-key": format.api_key}

        data = self._params_to_dict(format.parameters)

        file_data = {"file": ("filename", file_handle, FILETYPE_TO_MIMETYPE[filetype])}

        print("Try request!")
        response = requests.post(f"{format.api_url}/general/v0/general", headers=headers, data=data, files=file_data)
        response.raise_for_status()

        json_response = response.json()

        return self._render_markdown(json_response)

    def _read_file_locally(self, file_handle: IOBase, filetype: FileType) -> Optional[str]:
        _import_unstructured()
        if (not unstructured_partition_pdf) or (not unstructured_partition_docx) or (not unstructured_partition_pptx):
            # check whether unstructured library is actually available for better error message and to ensure proper typing (can't be None after this point)
            raise Exception("unstructured library is not available")

        file: Any = file_handle
        if filetype == FileType.PDF:
            # for PDF, read the file into a BytesIO object because some code paths in pdf parsing are doing an instance check on the file object and don't work with file-like objects
            file_handle.seek(0)
            with BytesIO(file_handle.read()) as file:
                file_handle.seek(0)
                elements = unstructured_partition_pdf(file=file)
        elif filetype == FileType.DOCX:
            elements = unstructured_partition_docx(file=file)
        elif filetype == FileType.PPTX:
            elements = unstructured_partition_pptx(file=file)

        return self._render_markdown([element.to_dict() for element in elements])

    def _handle_unprocessable_file(
        self, remote_file: RemoteFile, skip_unprocessable_file_types: Optional[bool], logger: logging.Logger
    ) -> None:
        if skip_unprocessable_file_types:
            logger.warn(f"File {remote_file.uri} cannot be parsed. Skipping it.")
        else:
            raise RecordParseError(FileBasedSourceError.ERROR_PARSING_RECORD, filename=remote_file.uri)

    def _get_filetype(self, file: IOBase, remote_file: RemoteFile) -> Optional[FileType]:
        """
        Detect the file type based on the file name and the file content.

        There are three strategies to determine the file type:
        1. Use the mime type if available (only some sources support it)
        2. Use the file name if available
        3. Use the file content
        """
        if remote_file.mime_type and remote_file.mime_type in STR_TO_FILETYPE:
            return STR_TO_FILETYPE[remote_file.mime_type]

        # set name to none, otherwise unstructured will try to get the modified date from the local file system
        if hasattr(file, "name"):
            file.name = None

        # detect_filetype is either using the file name or file content
        # if possible, try to leverage the file name to detect the file type
        # if the file name is not available, use the file content
        file_type = detect_filetype(
            filename=remote_file.uri,
        )
        if file_type is not None and not file_type == FileType.UNK:
            return file_type

        type_based_on_content = detect_filetype(file=file)

        # detect_filetype is reading to read the file content
        file.seek(0)

        return type_based_on_content

    def _supported_file_types(self) -> List[Any]:
        return [FileType.MD, FileType.PDF, FileType.DOCX, FileType.PPTX]

    def _render_markdown(self, elements: List[Dict[str, Any]]) -> str:
        return "\n\n".join((self._convert_to_markdown(el) for el in elements))

    def _convert_to_markdown(self, el: Dict[str, Any]) -> str:
        if dpath.util.get(el, "type") == "Title":
            heading_str = "#" * (dpath.util.get(el, "metadata/category_depth", default=1) or 1)
            return f"{heading_str} {dpath.util.get(el, 'text')}"
        elif dpath.util.get(el, "type") == "ListItem":
            return f"- {dpath.util.get(el, 'text')}"
        elif dpath.util.get(el, "type") == "Formula":
            return f"```\n{dpath.util.get(el, 'text')}\n```"
        else:
            return str(dpath.util.get(el, "text", default=""))

    @property
    def file_read_mode(self) -> FileReadMode:
        return FileReadMode.READ_BINARY


def _extract_format(config: FileBasedStreamConfig) -> UnstructuredFormat:
    config_format = config.format
    if not isinstance(config_format, UnstructuredFormat):
        raise ValueError(f"Invalid format config: {config_format}")
    return config_format
