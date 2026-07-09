import datetime
import os
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
def create_ssl(cert_file, key_file):
    key=rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    with open(key_file, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    subject=x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "localhost")])
    cert=x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(datetime.datetime.now()).not_valid_after(datetime.datetime.now() + datetime.timedelta(days=365)).add_extension(x509.SubjectAlternativeName([x509.DNSName(u"localhost")]), critical=False).sign(key, hashes.SHA256(), default_backend())
    with open(cert_file, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
os.makedirs("certs", exist_ok=True)
create_ssl("certs/cert.pem", "certs/key.pem")