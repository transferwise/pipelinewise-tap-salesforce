import time
import singer
import singer.metrics as metrics
import singer.utils as singer_utils
from singer import Transformer
from tap_salesforce.salesforce.bulk import Bulk
from tap_salesforce.salesforce.exceptions import TapSalesforceException

LOGGER = singer.get_logger()

BLACKLISTED_FIELDS = set(['attributes'])

def remove_blacklisted_fields(data):
    return {k: v for k, v in data.items() if k not in BLACKLISTED_FIELDS}

# pylint: disable=unused-argument
def transform_bulk_data_hook(data, typ, schema):
    result = data
    if isinstance(data, dict):
        result = remove_blacklisted_fields(data)

    # Salesforce Bulk API returns CSV's with empty strings for text fields.
    # When the text field is nillable and the data value is an empty string,
    # change the data so that it is None.
    if data == "" and "null" in schema['type']:
        result = None

    return result

def get_stream_version(catalog_entry, state):
    tap_stream_id = catalog_entry['tap_stream_id']
    replication_key = catalog_entry.get('replication_key')

    stream_version = (singer.get_bookmark(state, tap_stream_id, 'version') or
                      int(time.time() * 1000))
    if replication_key:
        return stream_version
    return int(time.time() * 1000)

def resume_syncing_bulk_query(sf, catalog_entry, job_id, state, counter):
    bulk = Bulk(sf)
    current_bookmark = singer.get_bookmark(state, catalog_entry['tap_stream_id'], 'JobHighestBookmarkSeen') or sf.get_start_date(state, catalog_entry)
    current_bookmark = singer_utils.strptime_with_tz(current_bookmark)
    batch_ids = singer.get_bookmark(state, catalog_entry['tap_stream_id'], 'BatchIDs')

    start_time = singer_utils.now()
    stream = catalog_entry['stream']
    stream_alias = catalog_entry.get('stream_alias')
    replication_key = catalog_entry.get('replication_key')
    stream_version = get_stream_version(catalog_entry, state)
    schema = catalog_entry['schema']

    # Iterate over the remaining batches, removing them once they are synced
    with Transformer(pre_hook=transform_bulk_data_hook) as transformer:
        for batch_id in batch_ids[:]:
            for rec in bulk.get_batch_results(job_id, batch_id, catalog_entry):
                counter.increment()
                rec = transformer.transform(rec, schema)
                rec = fix_record_anytype(rec, schema)
                singer.write_message(
                    singer.RecordMessage(
                        stream=(
                            stream_alias or stream),
                        record=rec,
                        version=stream_version,
                        time_extracted=start_time))

                # Update bookmark if necessary
                replication_key_value = replication_key and singer_utils.strptime_with_tz(rec[replication_key])
                if replication_key_value and replication_key_value <= start_time and replication_key_value > current_bookmark:
                    current_bookmark = singer_utils.strptime_with_tz(rec[replication_key])

            state = singer.write_bookmark(state,
                                          catalog_entry['tap_stream_id'],
                                          'JobHighestBookmarkSeen',
                                          singer_utils.strftime(current_bookmark))
            batch_ids.remove(batch_id)
            singer.write_state(state)

    return counter

def sync_stream(sf, catalog_entry, state):
    stream = catalog_entry['stream']

    with metrics.record_counter(stream) as counter:
        try:
            sync_records(sf, catalog_entry, state, counter)
            singer.write_state(state)
        except TapSalesforceException as ex:
            raise type(ex)("Error syncing {}: {}".format(
                stream, ex))
        except Exception as ex:
            raise Exception(
                "Unexpected error syncing {}: {}".format(
                    stream, ex)) from ex

        return counter

def sync_records(sf, catalog_entry, state, counter):
    chunked_bookmark = singer_utils.strptime_with_tz(sf.get_start_date(state, catalog_entry))
    stream = catalog_entry['stream']
    schema = catalog_entry['schema']
    stream_alias = catalog_entry.get('stream_alias')
    replication_key = catalog_entry.get('replication_key')
    stream_version = get_stream_version(catalog_entry, state)
    activate_version_message = singer.ActivateVersionMessage(stream=(stream_alias or stream),
                                                             version=stream_version)

    start_time = singer_utils.now()

    LOGGER.info('Syncing Salesforce data for stream %s', stream)
    with Transformer(pre_hook=transform_bulk_data_hook) as transformer:
        for rec in sf.query(catalog_entry, state):
            counter.increment()
            rec = transformer.transform(rec, schema)
            rec = fix_record_anytype(rec, schema)
            singer.write_message(
                singer.RecordMessage(
                    stream=(
                        stream_alias or stream),
                    record=rec,
                    version=stream_version,
                    time_extracted=start_time))

            replication_key_value = replication_key and singer_utils.strptime_with_tz(rec[replication_key])

            if sf.pk_chunking:
                if replication_key_value and replication_key_value <= start_time and replication_key_value > chunked_bookmark:
                    # Replace the highest seen bookmark and save the state in case we need to resume later
                    chunked_bookmark = singer_utils.strptime_with_tz(rec[replication_key])
                    state = singer.write_bookmark(
                        state,
                        catalog_entry['tap_stream_id'],
                        'JobHighestBookmarkSeen',
                        singer_utils.strftime(chunked_bookmark))
                    singer.write_state(state)
            # Before writing a bookmark, make sure Salesforce has not given us a
            # record with one outside our range
            elif replication_key_value and replication_key_value <= start_time:
                state = singer.write_bookmark(
                    state,
                    catalog_entry['tap_stream_id'],
                    replication_key,
                    rec[replication_key])
                singer.write_state(state)

        # Tables with no replication_key will send an
        # activate_version message for the next sync
        if not replication_key:
            singer.write_message(activate_version_message)
            state = singer.write_bookmark(
                state, catalog_entry['tap_stream_id'], 'version', None)

        # If pk_chunking is set, only write a bookmark at the end
        if sf.pk_chunking:
            # Write a bookmark with the highest value we've seen
            state = singer.write_bookmark(
                state,
                catalog_entry['tap_stream_id'],
                replication_key,
                singer_utils.strptime(chunked_bookmark))

def fix_record_anytype(rec, schema):
    """Modifies a record when the schema has no 'type' element due to a SF type of 'anyType.'
    Attempts to set the record's value for that element to an int, float, or string."""
    def try_cast(val, coercion):
        try:
            return coercion(val)
        except BaseException:
            return val

    for k, v in rec.items():
        if schema['properties'][k].get("type") is None:
            val = v
            val = try_cast(v, int)
            val = try_cast(v, float)
            if v in ["true", "false"]:
                val = (v == "true")

            if v == "":
                val = None

            rec[k] = val

    return rec