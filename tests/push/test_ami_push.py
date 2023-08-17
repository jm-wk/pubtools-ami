import os
import re
import shutil
import pytest
import json
import yaml
from logging import DEBUG
from collections import OrderedDict
from mock import patch, MagicMock
from pubtools._ami.tasks.push import AmiPush, entry_point, LOG

AMI_STAGE_ROOT = "/tmp/aws_staged"  # nosec B108
AMI_SOURCE = "staged:%s" % AMI_STAGE_ROOT


def compare_metadata(metadata, exp_metadata):
    """
    Helper fction to compare metadata object with a dictionary of expected metadata
    """
    result = True
    for key, value in exp_metadata.items():
        if getattr(metadata, key) != value:
            result = False
    return result

@pytest.fixture(scope="session", autouse=True)
def stage_ami():
    if os.path.exists(AMI_STAGE_ROOT):
        shutil.rmtree(AMI_STAGE_ROOT)
    ami_dest = os.path.join(AMI_STAGE_ROOT, "region-1-hourly/AWS_IMAGES")
    os.makedirs(ami_dest, mode=0o777)
    open(os.path.join(ami_dest, "ami-1.raw"), "a").close()

    j_file = os.path.join(os.path.dirname(__file__), "data/aws_staged/pub-mapfile.json")
    with open(j_file, "r") as in_file:
        with open(os.path.join(AMI_STAGE_ROOT, "pub-mapfile.json"), "w") as out_file:
            data = json.load(in_file)
            json.dump(data, out_file)
    yield

    if os.path.exists(AMI_STAGE_ROOT):
        shutil.rmtree(AMI_STAGE_ROOT)


accounts = json.dumps({"default": {"access-1": "secret-1"}})
region_acc = json.dumps(
    {"region-1": {"access-r": "secret-r"}, "default": {"access-1": "secret-1"}}
)
snapshot_acc = json.dumps(
    {
        "region-1": ["0987654321", "1234567890", "684062674729"],
        "default": ["1300655506"],
    }
)


@pytest.fixture
def staged_file():
    staged_yaml = {
        "header": {"version": "0.2"},
        "payload": {
            "files": [
                {"filename": "test.txt", "relative_path": "test_x86_64/FILES/test.txt"}
            ]
        },
    }
    temp_stage = "/tmp/test_staged"  # nosec B108
    if os.path.exists(temp_stage):
        shutil.rmtree(temp_stage)
    os.makedirs(os.path.join(temp_stage, "test_x86_64/FILES"), mode=0o777)
    open(os.path.join(temp_stage, "test_x86_64/FILES/test.txt"), "a").close()
    with open(os.path.join(temp_stage, "staged.yml"), "w") as out_file:
        yaml.dump(staged_yaml, out_file)
    yield temp_stage
    if os.path.exists(temp_stage):
        shutil.rmtree(temp_stage)


@pytest.fixture(autouse=True)
def mock_aws_publish():
    with patch("pubtools._ami.services.aws.AWSService.publish") as m:
        publish_rv = MagicMock(id="ami-1234567")
        publish_rv.name = "ami-rhel"
        m.return_value = publish_rv
        yield m


@pytest.fixture(autouse=True)
def mock_rhsm_api(requests_mocker):
    requests_mocker.register_uri(
        "GET",
        re.compile("amazon/provider_image_groups"),
        json={"body": [{"name": "RHEL_HOURLY", "providerShortName": "awstest"}]},
    )
    requests_mocker.register_uri("POST", re.compile("amazon/region"))
    requests_mocker.register_uri("PUT", re.compile("amazon/amis"))
    requests_mocker.register_uri("POST", re.compile("amazon/amis"))


@pytest.fixture(autouse=True)
def mock_debug_logger():
    # dicts are unordered in < py36. Hence, logging them may generate
    # a different order every time. The dicts are logged via debug
    # method in the code. LOG.debug is overridden to sort the dicts
    # before logging for the tests, generating a consistent sequence
    # of items everytime to match with the test_log data.
    def _log_debug(*args):
        debug_args = []
        for arg in args:
            if isinstance(arg, dict):
                od = OrderedDict(sorted(arg.items()))
                debug_args.append(json.dumps(od))
            else:
                debug_args.append(arg)
        LOG._log(DEBUG, debug_args[0], tuple(debug_args[1:]))

    with patch("pubtools._ami.tasks.push.LOG.debug") as log_debug:
        log_debug.side_effect = _log_debug
        yield log_debug


def test_do_push(command_tester, requests_mocker, mock_aws_publish, fake_collector):
    """Successful push and ship of an image that's not present on RHSM"""
    requests_mocker.register_uri("PUT", re.compile("amazon/amis"), status_code=400)
    command_tester.test(
        lambda: entry_point(AmiPush),
        [
            "test-push",
            "--rhsm-url",
            "https://example.com",
            "--aws-provider-name",
            "awstest",
            "--retry-wait",
            "1",
            "--accounts",
            region_acc,
            "--aws-access-id",
            "access_id",
            "--aws-secret-key",
            "secret_key",
            "--snapshot-account-ids",
            snapshot_acc,
            "--ship",
            "--debug",
            AMI_SOURCE,
        ],
    )

    # Check that aws publish has been called twice
    mock_aws_publish.assert_called_once()

    # Assert that correct metadata was used
    expected_metadata = {
        "ena_support": True,
        "sriov_net_support": "simple",
        "billing_products": ["code-0001"],
        "image_path": "/tmp/aws_staged/region-1-hourly/AWS_IMAGES/ami-1.raw",
        "image_name": "RHEL-8.5-RHEL-8.5.0_HVM_BETA-20210902-x86_64-5-Hourly2-GP2",
        "snapshot_name": "RHEL-8.5-RHEL-8.5.0_HVM_BETA-20210902-x86_64-5-Hourly2-GP2",
        "snapshot_account_ids": ["0987654321", "1234567890", "684062674729"],
        "description": "Provided by Red Hat, Inc.",
        "container": "redhat-cloudimg-region-1",
        "arch": "x86_64",
        "virt_type": "hvm",
        "root_device_name": "/dev/sda1",
        "volume_type": "gp2",
        "accounts": ["secret-r"],
        "groups": [],
        "tags": None,
    }
    aws_publish_args, _ = mock_aws_publish.call_args_list[0]
    aws_metadata = aws_publish_args[0]
    assert compare_metadata(aws_metadata, expected_metadata)

    # Check state of items pushed
    stored_items = fake_collector.items
    assert len(stored_items) == 1
    assert "PUSHED" == stored_items[0]["state"]

    # Check contents of files pushed
    images_json = json.loads(fake_collector.file_content["images.json"])
    assert len(images_json) == 1
    assert "ami-1234567" == images_json[0]["ami"]


def test_no_source(command_tester, capsys):
    """Checks that exception is raised when the source is missing"""
    command_tester.test(
        lambda: entry_point(AmiPush),
        ["test-push", "--debug", "--rhsm-url", "https://example.com"],
    )
    _, err = capsys.readouterr()
    assert (
        "error: too few arguments"
        or "error: the following arguments are required" in err
    )


def test_no_rhsm_url(command_tester, caplog):
    """Raises an error that RHSM url is not provided"""
    command_tester.test(
        lambda: entry_point(AmiPush),
        ["test-push", "--debug", AMI_SOURCE],
    )


def test_no_aws_credentials(command_tester):
    """Raises an error that AWS credentials were not provided to upload an image"""
    command_tester.test(
        lambda: entry_point(AmiPush),
        [
            "test-push",
            "--debug",
            "--rhsm-url",
            "https://example.com",
            "--aws-provider-name",
            "awstest",
            "--accounts",
            accounts,
            "--snapshot-account-ids",
            snapshot_acc,
            "--retry-wait",
            "1",
            AMI_SOURCE,
        ],
    )


def test_missing_product(command_tester):
    """Raises an error when the product the image is realted to is missing on RHSM"""
    command_tester.test(
        lambda: entry_point(AmiPush),
        [
            "test-push",
            "--rhsm-url",
            "https://example.com",
            "--aws-provider-name",
            "AWS",
            "--retry-wait",
            "1",
            "--aws-access-id",
            "access_id",
            "--aws-secret-key",
            "secret_key",
            "--debug",
            AMI_SOURCE,
        ],
    )


def test_push_public_image(
    command_tester, requests_mocker, mock_aws_publish, fake_collector
):
    """Successfully pushed images to all the accounts so it's available for general public"""
    command_tester.test(
        lambda: entry_point(AmiPush),
        [
            "test-push",
            "--rhsm-url",
            "https://example.com",
            "--aws-provider-name",
            "awstest",
            "--retry-wait",
            "1",
            "--accounts",
            accounts,
            "--snapshot-account-ids",
            snapshot_acc,
            "--aws-access-id",
            "access_id",
            "--aws-secret-key",
            "secret_key",
            "--ship",
            "--allow-public-image",
            "--debug",
            AMI_SOURCE,
        ],
    )

    # Check that aws publish has been called twice
    assert len(mock_aws_publish.call_args_list) == 2

    # Assert that correct metadata was used
    expected_metadata = {
        "ena_support": True,
        "sriov_net_support": "simple",
        "billing_products": ["code-0001"],
        "image_path": "/tmp/aws_staged/region-1-hourly/AWS_IMAGES/ami-1.raw",
        "image_name": "RHEL-8.5-RHEL-8.5.0_HVM_BETA-20210902-x86_64-5-Hourly2-GP2",
        "snapshot_name": "RHEL-8.5-RHEL-8.5.0_HVM_BETA-20210902-x86_64-5-Hourly2-GP2",
        "snapshot_account_ids": ["0987654321", "1234567890", "684062674729"],
        "description": "Provided by Red Hat, Inc.",
        "container": "redhat-cloudimg-region-1",
        "arch": "x86_64",
        "virt_type": "hvm",
        "root_device_name": "/dev/sda1",
        "volume_type": "gp2",
        "accounts": ["secret-1"],
        "groups": ["all"],
        "tags": None,
    }
    aws_publish_args, _ = mock_aws_publish.call_args_list[0]
    aws_metadata = aws_publish_args[0]
    assert compare_metadata(aws_metadata, expected_metadata)

    # Check state of items pushed
    stored_items = fake_collector.items
    assert len(stored_items) == 1
    assert "PUSHED" == stored_items[0]["state"]

    # Check contents of files pushed
    images_json = json.loads(fake_collector.file_content["images.json"])
    assert len(images_json) == 1
    assert "ami-1234567" == images_json[0]["ami"]


def test_create_region_failure(command_tester, requests_mocker):
    """Push fails when the region couldn't be created on RHSM"""
    requests_mocker.register_uri("POST", re.compile("amazon/region"), status_code=500)
    command_tester.test(
        lambda: entry_point(AmiPush),
        [
            "test-push",
            "--rhsm-url",
            "https://example.com",
            "--aws-provider-name",
            "awstest",
            "--retry-wait",
            "1",
            "--accounts",
            accounts,
            "--snapshot-account-ids",
            snapshot_acc,
            "--aws-access-id",
            "access_id",
            "--aws-secret-key",
            "secret_key",
            "--ship",
            "--debug",
            AMI_SOURCE,
        ],
    )


def test_create_image_failure(command_tester, requests_mocker):
    """Push fails if the image metadata couldn't be created on RHSM for a new image"""
    requests_mocker.register_uri("PUT", re.compile("amazon/amis"), status_code=400)
    requests_mocker.register_uri("POST", re.compile("amazon/amis"), status_code=500)
    command_tester.test(
        lambda: entry_point(AmiPush),
        [
            "test-push",
            "--rhsm-url",
            "https://example.com",
            "--aws-provider-name",
            "awstest",
            "--retry-wait",
            "1",
            "--max-retries",
            "2",
            "--accounts",
            accounts,
            "--snapshot-account-ids",
            snapshot_acc,
            "--aws-access-id",
            "access_id",
            "--aws-secret-key",
            "secret_key",
            "--ship",
            "--debug",
            AMI_SOURCE,
        ],
    )


def test_not_ami_push_item(command_tester, staged_file):
    """Non AMI pushitem is skipped from inclusion in push list"""
    temp_stage = "staged:%s" % staged_file

    command_tester.test(
        lambda: entry_point(AmiPush),
        [
            "test-push",
            "--rhsm-url",
            "https://example.com",
            "--aws-provider-name",
            "awstest",
            "--retry-wait",
            "1",
            "--max-retries",
            "2",
            "--accounts",
            accounts,
            "--aws-access-id",
            "access_id",
            "--aws-secret-key",
            "secret_key",
            "--debug",
            temp_stage,
        ],
    )


def test_aws_publish_failure_retry(
    command_tester, requests_mocker, mock_aws_publish, fake_collector
):
    """Image upload to AWS is retried on upload failure till it's pushed successfully
    or reached max retry count"""
    response = mock_aws_publish.return_value
    mock_aws_publish.side_effect = [
        Exception("Unable to publish"),
        response,
        Exception("Unable to publish"),
        response,
        response,
    ]
    command_tester.test(
        lambda: entry_point(AmiPush),
        [
            "test-push",
            "--rhsm-url",
            "https://example.com",
            "--aws-provider-name",
            "awstest",
            "--retry-wait",
            "1",
            "--accounts",
            accounts,
            "--snapshot-account-ids",
            snapshot_acc,
            "--aws-access-id",
            "access_id",
            "--aws-secret-key",
            "secret_key",
            "--ship",
            "--allow-public-image",
            "--debug",
            AMI_SOURCE,
        ],
    )

    # Check that aws publish has been called 5x
    assert len(mock_aws_publish.call_args_list) == 5

    # Assert that correct metadata was used
    expected_metadata = {
        "ena_support": True,
        "sriov_net_support": "simple",
        "billing_products": ["code-0001"],
        "image_path": "/tmp/aws_staged/region-1-hourly/AWS_IMAGES/ami-1.raw",
        "image_name": "RHEL-8.5-RHEL-8.5.0_HVM_BETA-20210902-x86_64-5-Hourly2-GP2",
        "snapshot_name": "RHEL-8.5-RHEL-8.5.0_HVM_BETA-20210902-x86_64-5-Hourly2-GP2",
        "snapshot_account_ids": ["0987654321", "1234567890", "684062674729"],
        "description": "Provided by Red Hat, Inc.",
        "container": "redhat-cloudimg-region-1",
        "arch": "x86_64",
        "virt_type": "hvm",
        "root_device_name": "/dev/sda1",
        "volume_type": "gp2",
        "accounts": ["secret-1"],
        "groups": [],
        "tags": None,
    }
    aws_publish_args, _ = mock_aws_publish.call_args_list[0]
    aws_metadata = aws_publish_args[0]
    assert compare_metadata(aws_metadata, expected_metadata)

    # Check state of items pushed
    stored_items = fake_collector.items
    assert len(stored_items) == 1
    assert "PUSHED" == stored_items[0]["state"]

    # Check contents of files pushed
    images_json = json.loads(fake_collector.file_content["images.json"])
    assert len(images_json) == 1
    assert "ami-1234567" == images_json[0]["ami"]
