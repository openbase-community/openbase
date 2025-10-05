from __future__ import annotations

import datetime
import decimal
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import rest_framework
from rest_framework import serializers
from rest_framework_dataclasses import fields
from rest_framework_dataclasses.serializers import DataclassSerializer

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from rest_framework_dataclasses.serializers import SerializerField


class BasicSourceFileSerializer(serializers.Serializer):
    path = rest_framework.fields.CharField()
    name = rest_framework.fields.CharField()
    app_name = rest_framework.fields.CharField()
    package_name = rest_framework.fields.CharField()


class BaseDataclassSerializer(DataclassSerializer):
    serializer_field_mapping: Mapping[type, type[SerializerField]] = {
        int: rest_framework.fields.IntegerField,
        float: rest_framework.fields.FloatField,
        bool: rest_framework.fields.BooleanField,
        str: rest_framework.fields.CharField,
        decimal.Decimal: fields.DefaultDecimalField,
        datetime.date: rest_framework.fields.DateField,
        datetime.datetime: rest_framework.fields.DateTimeField,
        datetime.time: rest_framework.fields.TimeField,
        datetime.timedelta: rest_framework.fields.DurationField,
        uuid.UUID: rest_framework.fields.UUIDField,
        list: fields.IterableField,
        dict: fields.MappingField,
        Path: rest_framework.fields.CharField,
    }

    def get_field_names(self) -> Iterable[str]:
        super_fields = list(super().get_field_names())
        if "objects" in super_fields:
            super_fields.remove("objects")
        return ["name", *super_fields]

    def update(self, instance, validated_data):
        super().update(instance, validated_data)
        instance.save()
        return instance
