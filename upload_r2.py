import os
import re
from contextlib import closing

from build_snapshot import FILENAMES, VERSION, decode, digest, iso_time, require, validate


POINTER = "v1/manifest.json"
BASELINE = "v1/input/baseline.json"
MAX_BYTES = 24_000_000


class StoreError(RuntimeError):
    pass


class R2:
    def __init__(self):
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError

        account = os.environ["R2_ACCOUNT_ID"]
        require(re.fullmatch(r"[a-f0-9]{32}", account), "invalid R2 account id")
        self.bucket = os.environ["R2_BUCKET"]
        self.client_error = ClientError
        self.transport_error = BotoCoreError
        self.client = boto3.client(
            "s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com", region_name="auto",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            config=Config(s3={"addressing_style": "path"}, request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )

    def storage_error(self, error):
        status = error.response["ResponseMetadata"]["HTTPStatusCode"]
        if status == 412:
            return StoreError("R2 object changed during publishing; rerun against the current manifest")
        return StoreError(f"R2 returned HTTP {status}")

    def get(self, key):
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=key)
            with closing(result["Body"]) as stream:
                body = stream.read(MAX_BYTES + 1)
        except self.client_error as error:
            if error.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                return None, None
            raise self.storage_error(error) from None
        except self.transport_error as error:
            raise StoreError(f"R2 response interrupted ({type(error).__name__})") from None
        require(len(body) <= MAX_BYTES, "oversized R2 object")
        return body, result["ETag"]

    def put(self, key, body, **condition):
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=body, ContentType="application/json",
                CacheControl="private, no-store", **condition,
            )
        except self.client_error as error:
            raise self.storage_error(error) from None
        except self.transport_error as error:
            raise StoreError(f"R2 write interrupted ({type(error).__name__})") from None

    def next_version(self, today):
        prefix = f"{today:%Y.%m.%d}."
        folders = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=f"v1/snapshot/{prefix}", Delimiter="/",
        ).search("CommonPrefixes[].Prefix")
        sequences = []
        try:
            for folder in folders:
                if not folder:
                    continue
                version = folder.removeprefix("v1/snapshot/").removesuffix("/")
                if VERSION.fullmatch(version):
                    sequences.append(int(version.split(".")[-1]))
        except self.client_error as error:
            raise self.storage_error(error) from None
        except self.transport_error as error:
            raise StoreError(f"R2 listing interrupted ({type(error).__name__})") from None
        return f"{prefix}{max(sequences, default=0) + 1}"


def read_previous(store):
    body, etag = store.get(POINTER)
    if body is None:
        return None, None, None
    manifest = decode(body)
    version = manifest["version"]
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1 and VERSION.fullmatch(version) and iso_time(manifest["built"]), "invalid live manifest")
    require(set(manifest["files"]) == set(FILENAMES), "live manifest roles mismatch")
    files = {}
    for role, filename in FILENAMES.items():
        entry = manifest["files"][role]
        key = f"v1/snapshot/{version}/{filename}"
        require(entry["key"] == key, "invalid live snapshot path")
        body, _ = store.get(key)
        require(body is not None and digest(body) == entry["sha256"], "live snapshot file is missing or corrupt")
        files[role] = decode(body)
    validate(files, manifest["count"], allow_legacy_app_fields=True)
    return manifest, files, etag
