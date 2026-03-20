from __future__ import unicode_literals

import datetime
import os
import random
import re
import string
import mimetypes

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

import frappe

try:
    import magic
except ImportError:
    magic = None


class S3Operations:
    def __init__(self):
        """Initialize S3 settings from Frappe 'S3 File Attachment' DocType"""
        self.s3_settings_doc = frappe.get_doc(
            'S3 File Attachment', 'S3 File Attachment'
        )
        endpoint_url = self.s3_settings_doc.get('endpoint_url')

        # Initialize S3 client
        if self.s3_settings_doc.aws_key and self.s3_settings_doc.aws_secret:
            self.S3_CLIENT = boto3.client(
                's3',
                aws_access_key_id=self.s3_settings_doc.aws_key,
                aws_secret_access_key=self.s3_settings_doc.aws_secret,
                region_name=self.s3_settings_doc.region_name,
                config=Config(signature_version='s3v4'),
                endpoint_url=endpoint_url or None
            )
        else:
            self.S3_CLIENT = boto3.client(
                's3',
                region_name=self.s3_settings_doc.region_name,
                config=Config(signature_version='s3v4'),
                endpoint_url=endpoint_url or None
            )

        self.BUCKET = self.s3_settings_doc.bucket_name
        self.folder_name = self.s3_settings_doc.folder_name or "uploads"

    def strip_special_chars(self, file_name):
        """Strip characters not matching regex"""
        regex = re.compile('[^0-9a-zA-Z._-]')
        return regex.sub('', file_name)

    def key_generator(self, file_name=None, parent_doctype=None, parent_name=None):
        """Generate a safe key for S3 objects"""
        # Call hook if available
        hook_cmd = frappe.get_hooks().get("s3_key_generator")
        if hook_cmd:
            try:
                k = frappe.get_attr(hook_cmd[0])(
                    file_name=file_name,
                    parent_doctype=parent_doctype,
                    parent_name=parent_name
                )
                if k:
                    return k.strip("/")
            except Exception:
                pass

        # Safe defaults
        file_name = (file_name or "file").replace(" ", "_")
        file_name = self.strip_special_chars(file_name)
        parent_doctype = parent_doctype or "Unattached"
        parent_name = parent_name or "NoName"

        key_random = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))

        today = datetime.datetime.now()
        year = today.strftime("%Y")
        month = today.strftime("%m")
        day = today.strftime("%d")

        parts = [
            self.folder_name,
            year,
            month,
            day,
            parent_doctype,
            f"{key_random}_{file_name}"
        ]
        return "/".join(parts)

    def upload_files_to_s3_with_key(self, file_path, file_name, is_private, parent_doctype, parent_name):
        """Upload file to S3 with safe key"""
        # Determine mime type safely
        try:
            if magic:
                mime_type = magic.from_file(file_path, mime=True)
            else:
                mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        except Exception:
            mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"

        key = self.key_generator(file_name, parent_doctype, parent_name)

        extra_args = {
            "ContentType": mime_type,
            "Metadata": {"file_name": file_name, "ContentType": mime_type}
        }
        if not is_private:
            extra_args["ACL"] = "public-read"

        try:
            self.S3_CLIENT.upload_file(file_path, self.BUCKET, key, ExtraArgs=extra_args)
        except boto3.exceptions.S3UploadFailedError:
            frappe.throw(frappe._("File Upload Failed. Please try again."))

        return key

    def delete_from_s3(self, key):
        """Delete file from S3"""
        if self.s3_settings_doc.delete_file_from_cloud:
            try:
                self.S3_CLIENT.delete_object(Bucket=self.BUCKET, Key=key)
            except ClientError:
                frappe.throw(frappe._("Access denied: Could not delete file"))

    def read_file_from_s3(self, key):
        """Read file from S3"""
        return self.S3_CLIENT.get_object(Bucket=self.BUCKET, Key=key)

    def get_url(self, key, file_name=None):
        """Generate signed URL or public URL"""
        expiry = getattr(self.s3_settings_doc, "signed_url_expiry_time", 120)
        params = {"Bucket": self.BUCKET, "Key": key}
        if file_name:
            params["ResponseContentDisposition"] = f'inline; filename="{file_name}"'

        url = self.S3_CLIENT.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=expiry
        )
        return url


@frappe.whitelist()
def file_upload_to_s3(doc, method):
    """Upload a Frappe File record to S3"""
    if doc.attached_to_doctype == "Prepared Report":
        return

    s3_upload = S3Operations()
    site_path = frappe.utils.get_site_path()
    parent_doctype = doc.attached_to_doctype or "File"
    parent_name = doc.attached_to_name or doc.name

    file_path = os.path.join(site_path, "public" + doc.file_url) if not doc.is_private else os.path.join(site_path, doc.file_url)

    if not os.path.exists(file_path):
        frappe.log_error(f"File path does not exist: {file_path}")
        return

    key = s3_upload.upload_files_to_s3_with_key(
        file_path, doc.file_name, doc.is_private, parent_doctype, parent_name
    )

    # Build file URL
    if doc.is_private:
        file_url = f"/api/method/frappe_s3_attachment.controller.generate_file?key={key}&file_name={doc.file_name}"
    else:
        base_url = s3_upload.S3_CLIENT.meta.endpoint_url or f"https://{s3_upload.BUCKET}.s3.amazonaws.com"
        file_url = f"{base_url}/{key}"

    # Remove local file safely
    try:
        os.remove(file_path)
    except Exception:
        frappe.log_error(f"Failed to remove local file: {file_path}")

    # Update File doc
    frappe.db.sql(
        """UPDATE `tabFile` SET file_url=%s, folder=%s, old_parent=%s, content_hash=%s WHERE name=%s""",
        (file_url, "Home/Attachments", "Home/Attachments", key, doc.name)
    )
    doc.file_url = file_url

    if parent_doctype and frappe.get_meta(parent_doctype).get("image_field"):
        frappe.db.set_value(
            parent_doctype,
            parent_name,
            frappe.get_meta(parent_doctype).get("image_field"),
            file_url
        )

    frappe.db.commit()


@frappe.whitelist()
def generate_file(key=None, file_name=None):
    """Stream file from S3 via redirect"""
    if key:
        s3_upload = S3Operations()
        signed_url = s3_upload.get_url(key, file_name)
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = signed_url
    else:
        frappe.local.response["body"] = "Key not found."
    return


def upload_existing_files_s3(name):
    """Upload all existing files to S3 safely"""
    doc = frappe.get_doc("File", name)
    if not doc:
        return

    s3_upload = S3Operations()
    site_path = frappe.utils.get_site_path()
    parent_doctype = doc.attached_to_doctype or "Unattached"
    parent_name = doc.attached_to_name or doc.name

    file_path = os.path.join(site_path, "public" + doc.file_url) if not doc.is_private else os.path.join(site_path, doc.file_url)

    if not os.path.exists(file_path):
        frappe.log_error(f"File path missing during migration: {file_path}")
        return

    key = s3_upload.upload_files_to_s3_with_key(
        file_path, doc.file_name, doc.is_private, parent_doctype, parent_name
    )

    if doc.is_private:
        file_url = f"/api/method/frappe_s3_attachment.controller.generate_file?key={key}"
    else:
        base_url = s3_upload.S3_CLIENT.meta.endpoint_url or f"https://{s3_upload.BUCKET}.s3.amazonaws.com"
        file_url = f"{base_url}/{key}"

    try:
        os.remove(file_path)
    except Exception:
        frappe.log_error(f"Failed to remove local file: {file_path}")

    frappe.db.sql(
        """UPDATE `tabFile` SET file_url=%s, folder=%s, old_parent=%s, content_hash=%s WHERE name=%s""",
        (file_url, "Home/Attachments", "Home/Attachments", key, doc.name),
    )
    frappe.db.commit()


def s3_file_regex_match(file_url):
    """Check if file URL already points to S3 or local endpoint"""
    return re.match(r"^(https:|/api/method/frappe_s3_attachment.controller.generate_file)", file_url)


@frappe.whitelist()
def migrate_existing_files():
    """Migrate all existing files to S3 safely"""
    files_list = frappe.get_all("File", fields=["name", "file_url"])
    for file in files_list:
        if file["file_url"] and not s3_file_regex_match(file["file_url"]):
            upload_existing_files_s3(file["name"])
    return True


def delete_from_cloud(doc, method):
    """Delete file from s3"""
    s3 = S3Operations()
    s3.delete_from_s3(doc.content_hash)


@frappe.whitelist()
def ping():
    """
    Test function to check if api function work.
    """
    return "pong"
