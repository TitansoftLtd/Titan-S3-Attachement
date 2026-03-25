from __future__ import unicode_literals

import datetime
import os
import random
import re
import string

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

import frappe
import magic


class S3Operations:
    def __init__(self):
        self.s3_settings_doc = frappe.get_doc(
            "S3 File Attachment",
            "S3 File Attachment",
        )

        endpoint_url = self.s3_settings_doc.get("endpoint_url")

        self.S3_CLIENT = boto3.client(
            "s3",
            aws_access_key_id=self.s3_settings_doc.aws_key or None,
            aws_secret_access_key=self.s3_settings_doc.aws_secret or None,
            region_name=self.s3_settings_doc.region_name,
            endpoint_url=endpoint_url or None,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3}
            ),
        )

        self.BUCKET = self.s3_settings_doc.bucket_name
        self.folder_name = self.s3_settings_doc.folder_name

    # -------------------------
    # Utilities
    # -------------------------

    def strip_special_chars(self, file_name):
        return re.sub(r"[^0-9a-zA-Z._-]", "", file_name)

    def key_generator(self, file_name, parent_doctype, parent_name):
        hook_cmd = frappe.get_hooks().get("s3_key_generator")

        if hook_cmd:
            try:
                key = frappe.get_attr(hook_cmd[0])(
                    file_name=file_name,
                    parent_doctype=parent_doctype,
                    parent_name=parent_name,
                )
                if key:
                    return key.strip("/")
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    "S3 Key Generator Failed"
                )

        file_name = (file_name or "file").replace(" ", "_")
        file_name = self.strip_special_chars(file_name)

        rand = "".join(
            random.choice(string.ascii_uppercase + string.digits)
            for _ in range(8)
        )

        today = datetime.datetime.now()

        path = f"{today:%Y/%m/%d}/{parent_doctype}/{rand}_{file_name}"

        if self.folder_name:
            return f"{self.folder_name}/{path}"

        return path

    # -------------------------
    # Core Operations
    # -------------------------

    def upload_file(self, file_path, file_name, is_private, parent_doctype, parent_name):
        if not os.path.exists(file_path):
            frappe.throw(f"File not found: {file_path}")

        mime_type = magic.from_file(file_path, mime=True)
        key = self.key_generator(file_name, parent_doctype, parent_name)

        extra_args = {
            "ContentType": mime_type,
            "Metadata": {
                "file_name": file_name
            },
            "ContentDisposition": f'inline; filename="{file_name}"'
        }

        if not is_private:
            extra_args["ACL"] = "public-read"

        try:
            self.S3_CLIENT.upload_file(
                file_path,
                self.BUCKET,
                key,
                ExtraArgs=extra_args,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "S3 Upload Failed")
            frappe.throw("File upload failed. Please try again.")

        return key

    def delete_file(self, key):
        if not self.s3_settings_doc.delete_file_from_cloud:
            return

        try:
            self.S3_CLIENT.delete_object(
                Bucket=self.BUCKET,
                Key=key
            )
        except ClientError:
            frappe.throw("Access denied: Could not delete file")

    def generate_url(self, key, file_name=None):
        expiry = self.s3_settings_doc.signed_url_expiry_time or 120

        params = {
            "Bucket": self.BUCKET,
            "Key": key,
        }

        if file_name:
            params["ResponseContentDisposition"] = f'inline; filename="{file_name}"'

        return self.S3_CLIENT.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expiry,
        )

    def build_public_url(self, key):
        if self.s3_settings_doc.endpoint_url:
            return f"{self.s3_settings_doc.endpoint_url}/{self.BUCKET}/{key}"
        return f"https://{self.BUCKET}.s3.amazonaws.com/{key}"


# -------------------------
# Hooks
# -------------------------

@frappe.whitelist()
def file_upload_to_s3(doc, method):
    if doc.attached_to_doctype == "Prepared Report":
        return

    ignore = frappe.local.conf.get("ignore_s3_upload_for_doctype") or ["Data Import"]

    parent_doctype = doc.attached_to_doctype or "File"
    parent_name = doc.attached_to_name or doc.name

    if parent_doctype in ignore:
        return

    s3 = S3Operations()

    site_path = frappe.utils.get_site_path()
    file_path = (
        site_path + (doc.file_url if doc.is_private else "/public" + doc.file_url)
    )

    file_name = doc.file_name or doc.name

    key = s3.upload_file(
        file_path,
        file_name,
        doc.is_private,
        parent_doctype,
        parent_name,
    )

    if doc.is_private:
        method ="frappe_s3_attachment.controller.generate_file"
        file_url = f"/api/method/{method}?key={key}&file_name={file_name}"
    else:
        file_url = s3.build_public_url(key)

    # Update DB FIRST
    frappe.db.set_value(
        "File",
        doc.name,
        {
            "file_url": file_url,
            "content_hash": key,
            "folder": "Home/Attachments",
            "old_parent": "Home/Attachments",
        },
        update_modified=False,
    )
    frappe.db.commit()

    # THEN delete local file
    if os.path.exists(file_path):
        os.remove(file_path)

    doc.file_url = file_url


@frappe.whitelist()
def generate_file(key=None, file_name=None):
    if not key:
        frappe.local.response["body"] = "Key not found."
        return

    s3 = S3Operations()
    signed_url = s3.generate_url(key, file_name)

    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = signed_url


def delete_from_cloud(doc, method):
    s3 = S3Operations()
    s3.delete_file(doc.content_hash)


# -------------------------
# Migration
# -------------------------

def is_s3_url(file_url):
    return bool(re.match(
        r"^(https://.*s3.*|/api/method/your_app.s3.generate_file)",
        file_url or ""
    ))


@frappe.whitelist()
def migrate_existing_files():
    files = frappe.get_all("File", fields=["name", "file_url"])

    for i, f in enumerate(files, 1):
        if not f.file_url or is_s3_url(f.file_url):
            continue

        upload_existing_file(f.name)

        if i % 100 == 0:
            frappe.logger().info(f"Migrated {i} files")

    return True


def upload_existing_file(name):
    doc = frappe.get_doc("File", name)

    site_path = frappe.utils.get_site_path()
    file_path = (
        site_path + (doc.file_url if doc.is_private else "/public" + doc.file_url)
    )

    if not os.path.exists(file_path):
        return

    s3 = S3Operations()

    key = s3.upload_file(
        file_path,
        doc.file_name or doc.name,
        doc.is_private,
        doc.attached_to_doctype or "File",
        doc.attached_to_name or doc.name,
    )

    if doc.is_private:
        method = "frappe_s3_attachment.controller.generate_file"
        file_url = f"/api/method/{method}?key={key}&file_name={doc.file_name or doc.name}"
    else:
        file_url = s3.build_public_url(key)

    frappe.db.set_value(
        "File",
        doc.name,
        {
            "file_url": file_url,
            "content_hash": key,
        },
    )
    frappe.db.commit()

    os.remove(file_path)


# -------------------------
# Test
# -------------------------

@frappe.whitelist()
def ping():
    return "pong"