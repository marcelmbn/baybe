"""
Telemetry  functionality for BayBE.
"""
import getpass
import hashlib
import os
import socket
from typing import Dict, Union

from opentelemetry._metrics import get_meter, set_meter_provider
from opentelemetry.exporter.otlp.proto.grpc._metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._metrics import MeterProvider
from opentelemetry.sdk._metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from .utils import strtobool

_instruments = {}
_resource = Resource.create({"service.namespace": "BayBE-test2", "service.name": "SDK"})
_reader = PeriodicExportingMetricReader(
    exporter=OTLPMetricExporter(
        endpoint="***REMOVED***.elb."
        "eu-central-1.amazonaws.com:4317",
        insecure=True,
    )
)
_provider = MeterProvider(resource=_resource, metric_readers=[_reader])
set_meter_provider(_provider)

# Setup Global Metric Provider
_meter = get_meter("aws-otel", "1.0")


def get_user_details() -> Dict[str, str]:
    """
    Generate a unique hash value for the current user based on the host name and
    uppercase username, e.g. the first 10 upper-case digits of the sha256
    hash of 'LTD1234M123132'.

    Returns
    -------
        dict: Contains the hostname and username in hashed format
    """
    username_hash = os.environ.get("BAYBE_DEBUG_FAKE_USERHASH", None) or (
        hashlib.sha256(getpass.getuser().upper().encode())
        .hexdigest()
        .upper()[:10]  # take only first 10 digits to enhance readability in dashboard
    )
    hostname_hash = os.environ.get("BAYBE_DEBUG_FAKE_HOSTHASH", None) or (
        hashlib.sha256(socket.gethostname().encode()).hexdigest().upper()[:10]
    )
    # Alternatively one could take the MAC address like hex(uuid.getnode())

    return {"host": hostname_hash, "user": username_hash}


def is_enabled() -> bool:
    """
    Tells whether telemetry currently is enabled. Telemetry can be disabled by setting
    the respective environment variable.

    Returns
    -------
        bool
    """
    return strtobool(os.environ.get("BAYBE_TELEMETRY_ENABLED", "true"))


def telemetry_record_value(
    instrument_name: str, value: Union[bool, int, float, str]
) -> None:
    """
    Transmits a given value under a given label to the telemetry backend. The values are
     recorded as histograms, i.e. the info about record time and sample size is also
     available. This can be used to count function calls (record the value 1) or
     statistics about any variable (record its value). Due to serialization limitations
     only certain data types of value are allowed.

    Parameters
    ----------
    instrument_name: str
        The label under which this statistic is logged.
    value
        The value of the statistic to be logged.

    Returns
    -------
        None
    """
    if is_enabled():
        if instrument_name in _instruments:
            histogram = _instruments[instrument_name]
        else:
            histogram = _meter.create_histogram(
                instrument_name,
                description=f"Histogram for instrument {instrument_name}",
            )
            _instruments[instrument_name] = histogram
        histogram.record(value, get_user_details())
