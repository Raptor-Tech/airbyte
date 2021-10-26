#
# Copyright (c) 2021 Airbyte, Inc., all rights reserved.
#


from abc import ABC
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional, Tuple

import requests
from airbyte_cdk.sources import AbstractSource
from airbyte_cdk.sources.streams import Stream
from airbyte_cdk.sources.streams.http import HttpStream
# from airbyte_cdk.sources.streams.http.auth import TokenAuthenticator
from airbyte_cdk.sources.streams.http.requests_native_auth import TokenAuthenticator

import math
from datetime import datetime, timedelta

"""
TODO: Most comments in this class are instructive and should be deleted after the source is implemented.

This file provides a stubbed example of how to use the Airbyte CDK to develop both a source connector which supports full refresh or and an
incremental syncs from an HTTP API.

The various TODOs are both implementation hints and steps - fulfilling all the TODOs should be sufficient to implement one basic and one incremental
stream from a source. This pattern is the same one used by Airbyte internally to implement connectors.

The approach here is not authoritative, and devs are free to use their own judgement.

There are additional required TODOs in the files within the integration_tests folder and the spec.json file.
"""


# Basic full refresh stream
class ZenloopStream(HttpStream, ABC):

    url_base = "https://api.zenloop.com/v1/"
    extra_params = None
    has_date_param = False

    def __init__(self, api_token: str, date_from: Optional[str], public_hash_id: Optional[str], **kwargs):
        super().__init__(authenticator=api_token)
        self.api_token=api_token
        self.date_from = date_from or datetime.today().strftime('%Y-%m-%d')
        self.public_hash_id = public_hash_id or None

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        decoded_response = response.json()
        page = decoded_response['meta']['page']
        per_page = decoded_response['meta']['per_page']
        total = decoded_response['meta']['total']

        if page < math.ceil(total/per_page):
            return {'page': page + 1}
        else:
            return None

    def request_params(
            self,
            stream_state: Mapping[str, Any],
            stream_slice: Mapping[str, Any] = None,
            next_page_token: Mapping[str, Any] = None,
    ) -> MutableMapping[str, Any]:
        if self.has_date_param:
            params = {"date_from": self.date_from}
        else:
            params = {}
        if self.extra_params:
            params.update(self.extra_params)
        if next_page_token:
            params.update(**next_page_token)
        return params

    def parse_response(self, response: requests.Response, **kwargs) -> Iterable[Mapping]:
        response_json = response.json()
        yield response_json

class ChildStreamMixin:
    parent_stream_class: Optional[ZenloopStream] = None

    def stream_slices(self, sync_mode,  stream_state: Mapping[str, Any] = None, **kwargs) -> Iterable[Optional[Mapping[str, any]]]:
        # loop through all public_hash_id's if None was provided
        # return nothing otherwise
        if not self.public_hash_id:
            for item in self.parent_stream_class(api_token=self.api_token, date_from = self.date_from, public_hash_id = self.public_hash_id).read_records(sync_mode=sync_mode):
                # set date_from to most current cursor_field or date_from if not incremental
                if stream_state:
                    date_from = stream_state[self.cursor_field]
                else:
                    date_from = self.date_from
                yield {"survey_id": item["public_hash_id"], "date_from": date_from}
        else:
            yield None

# Basic incremental stream
class IncrementalZenloopStream(ZenloopStream, ABC):
    # checkpoint stream reads after 100 records.
    state_checkpoint_interval = 100
    cursor_field = "inserted_at"

    def get_updated_state(self, current_stream_state: MutableMapping[str, Any], latest_record: Mapping[str, Any]) -> Mapping[str, Any]:
        # latest_record has objects in answers
        if len(latest_record) > 0:
            # add 1 second to not pull latest_record again
            latest_record_date = (datetime.strptime(latest_record[self.cursor_field], '%Y-%m-%dT%H:%M:%S.%fZ') + timedelta(seconds=1)).isoformat() + str('Z')
        else:
            latest_record_date = ""
        max_record = max(latest_record_date, current_stream_state.get(self.cursor_field, ""))
        return {self.cursor_field: max_record}

    def request_params(
        self, stream_state: Mapping[str, Any], stream_slice: Mapping[str, Any] = None, next_page_token: Mapping[str, Any] = None
    ) -> MutableMapping[str, Any]:
        params = super().request_params(stream_state, stream_slice, next_page_token)
        if stream_state:
            # if looped through all slices take its date_from parameter
            # else no public_hash_id provided -> take cursor_field
            if stream_slice:
                params["date_from"] = stream_slice["date_from"]
            else:
                params["date_from"] = stream_state[self.cursor_field]
        return params

class Surveys(ZenloopStream):
    # API Doc: https://docs.zenloop.com/reference#get-list-of-surveys
    primary_key = None
    has_date_param = False
    extra_params = {"page": "1"}

    def path(
        self,
        stream_state: Mapping[str, Any] = None,
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None
    ) -> str:
        return "surveys"

    def parse_response(self, response: requests.Response, **kwargs) -> Iterable[Mapping]:
        response_json = response.json()
        yield from response_json.get("surveys", [])

class Answers(ChildStreamMixin, IncrementalZenloopStream):
    # API Doc: https://docs.zenloop.com/reference#get-answers
    primary_key = "id"
    has_date_param = True
    parent_stream_class = Surveys
    extra_params = {"page": "1", "order_type": "desc", "order_by": "inserted_at", "date_shortcut": "custom", "date_to": datetime.today().strftime('%Y-%m-%d')}

    def path(
        self,
        stream_state: Mapping[str, Any] = None,
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None
    ) -> str:
        # take optional public_hash_id if entered
        if self.public_hash_id:
            return f"surveys/{self.public_hash_id}/answers"
        # slice all public_hash_id's if nothing provided
        else:
            return f"surveys/{stream_slice['survey_id']}/answers"

    def parse_response(self, response: requests.Response, **kwargs) -> Iterable[Mapping]:
        response_json = response.json()
        # select answers and surveys to be able to link answer to a survey
        yield from response_json.get("answers", [])

class SurveyGroups(ZenloopStream):
    # API Doc: https://docs.zenloop.com/reference#get-list-of-survey-groups
    primary_key = None
    has_date_param = False
    extra_params = {"page": "1"}

    def path(
        self,
        stream_state: Mapping[str, Any] = None,
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None
    ) -> str:
        return "survey_groups"

    def parse_response(self, response: requests.Response, **kwargs) -> Iterable[Mapping]:
        response_json = response.json()
        yield from response_json.get("survey_groups", [])

class AnswersSurveyGroup(ChildStreamMixin, IncrementalZenloopStream):
    # API Doc: https://docs.zenloop.com/reference#get-answers-for-survey-group
    primary_key = "id"
    has_date_param = True
    parent_stream_class = SurveyGroups
    extra_params = {"page": "1", "order_type": "desc", "order_by": "inserted_at", "date_shortcut": "custom", "date_to": datetime.today().strftime('%Y-%m-%d')}

    def path(
        self,
        stream_state: Mapping[str, Any] = None,
        stream_slice: Mapping[str, Any] = None,
        next_page_token: Mapping[str, Any] = None
    ) -> str:
        # take optional public_hash_id if entered
        if self.public_hash_id:
            return f"survey_groups/{self.public_hash_id}/answers"
        # slice all public_hash_id's if nothing provided
        else:
            return f"survey_groups/{stream_slice['survey_id']}/answers"

    def parse_response(self, response: requests.Response, **kwargs) -> Iterable[Mapping]:
        response_json = response.json()
        # select answers and surveys to be able to link answer to a survey
        yield from response_json.get("answers", [])

# Source
class SourceZenloop(AbstractSource):
    def check_connection(self, logger, config) -> Tuple[bool, any]:
        try:
            authenticator = TokenAuthenticator(config["api_token"])
            url = f"{ZenloopStream.url_base}surveys"

            session = requests.get(url, headers=authenticator.get_auth_header())
            session.raise_for_status()
            return True, None
        except Exception as error:
            return False, f"Unable to connect to Zenloop API with the provided credentials - {error}"

    def streams(self, config: Mapping[str, Any]) -> List[Stream]:
        args = {"api_token": TokenAuthenticator(token=config["api_token"]), "date_from": config["date_from"], "public_hash_id": config.get("public_hash_id")}
        return [Surveys(**args), Answers(**args), SurveyGroups(**args), AnswersSurveyGroup(**args)]