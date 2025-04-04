# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Methods that deal with local pandas/pyarrow dataframes."""

from __future__ import annotations

import dataclasses
import functools
import uuid

import pyarrow as pa

import bigframes.core.schema as schemata
import bigframes.dtypes


@dataclasses.dataclass(frozen=True)
class LocalTableMetadata:
    total_bytes: int
    row_count: int

    @classmethod
    def from_arrow(cls, table: pa.Table):
        return cls(total_bytes=table.nbytes, row_count=table.num_rows)


@dataclasses.dataclass(frozen=True)
class ManagedArrowTable:
    data: pa.Table = dataclasses.field(hash=False)
    schema: schemata.ArraySchema = dataclasses.field(hash=False)
    id: uuid.UUID = dataclasses.field(default_factory=uuid.uuid4)

    @functools.cached_property
    def metadata(self):
        return LocalTableMetadata.from_arrow(self.data)


def arrow_schema_to_bigframes(arrow_schema: pa.Schema) -> schemata.ArraySchema:
    """Infer the corresponding bigframes schema given a pyarrow schema."""
    schema_items = tuple(
        schemata.SchemaItem(
            field.name,
            bigframes_type_for_arrow_type(field.type),
        )
        for field in arrow_schema
    )
    return schemata.ArraySchema(schema_items)


def adapt_pa_table(arrow_table: pa.Table) -> pa.Table:
    """Adapt a pyarrow table to one that can be handled by bigframes. Converts tz to UTC and unit to us for temporal types."""
    new_schema = pa.schema(
        [
            pa.field(field.name, arrow_type_replacements(field.type))
            for field in arrow_table.schema
        ]
    )
    return arrow_table.cast(new_schema)


def bigframes_type_for_arrow_type(pa_type: pa.DataType) -> bigframes.dtypes.Dtype:
    return bigframes.dtypes.arrow_dtype_to_bigframes_dtype(
        arrow_type_replacements(pa_type)
    )


def arrow_type_replacements(type: pa.DataType) -> pa.DataType:
    if pa.types.is_timestamp(type):
        # This is potentially lossy, but BigFrames doesn't support ns
        new_tz = "UTC" if (type.tz is not None) else None
        return pa.timestamp(unit="us", tz=new_tz)
    if pa.types.is_time64(type):
        # This is potentially lossy, but BigFrames doesn't support ns
        return pa.time64("us")
    if pa.types.is_duration(type):
        # This is potentially lossy, but BigFrames doesn't support ns
        return pa.duration("us")
    if pa.types.is_decimal128(type):
        return pa.decimal128(38, 9)
    if pa.types.is_decimal256(type):
        return pa.decimal256(76, 38)
    if pa.types.is_dictionary(type):
        return arrow_type_replacements(type.value_type)
    if pa.types.is_large_string(type):
        # simple string type can handle the largest strings needed
        return pa.string()
    if pa.types.is_null(type):
        # null as a type not allowed, default type is float64 for bigframes
        return pa.float64()
    if pa.types.is_list(type):
        new_field_t = arrow_type_replacements(type.value_type)
        if new_field_t != type.value_type:
            return pa.list_(new_field_t)
        return type
    else:
        return type
