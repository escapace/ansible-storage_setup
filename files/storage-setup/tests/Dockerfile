FROM quay.io/centos/centos:stream10

RUN dnf -y install python3 xfsprogs util-linux-core && dnf clean all

COPY --chmod=0755 storage-setup.py /usr/bin/storage-setup
COPY --chmod=0644 tests/storage-setup.py /storage-setup-tests.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    STORAGE_SETUP_SCRIPT=/usr/bin/storage-setup \
    STORAGE_SETUP_TEST_CONFIG_DIRECTORY=/etc/storage-setup.d \
    STORAGE_SETUP_TEST_IMAGE_FORMATTING=1

USER 65534:65534
ENTRYPOINT ["/usr/bin/python3", "-W", "error", "/storage-setup-tests.py"]
