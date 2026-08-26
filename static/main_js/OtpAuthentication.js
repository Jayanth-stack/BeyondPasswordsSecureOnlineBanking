function setStatus(message, kind) {
    const status = document.getElementById('otpStatus');
    if (!status) {
        return;
    }
    status.textContent = message || '';
    status.classList.remove('error', 'success');
    if (kind) {
        status.classList.add(kind);
    }
}

function postJson(path, body) {
    return fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
        body: JSON.stringify(body),
    }).then(function (response) {
        if (response.redirected) {
            window.location.href = response.url;
            return null;
        }
        return response.json().then(function (data) {
            data._httpStatus = response.status;
            return data;
        });
    });
}

document.getElementById('verifyBtn').addEventListener('click', function () {
    const userEnteredOTP = document.getElementById('otp').value;
    const verifyButton = document.getElementById('verifyBtn');

    if (userEnteredOTP.trim() === '') {
        setStatus('Please enter the OTP.', 'error');
        return;
    }

    verifyButton.textContent = 'Verifying...';
    verifyButton.disabled = true;
    setStatus('Verifying OTP…');

    postJson('/verify-otp', { otp_code: userEnteredOTP })
        .then(function (data) {
            if (!data) {
                return;
            }
            if (data._httpStatus === 401 && (data.error || '').toLowerCase().indexOf('session') !== -1) {
                window.location.href = '/';
                return;
            }
            throw new Error(data.error || data.message || 'Incorrect OTP. Please try again.');
        })
        .catch(function (error) {
            setStatus(error.message, 'error');
        })
        .finally(function () {
            verifyButton.textContent = 'Verify OTP';
            verifyButton.disabled = false;
        });
});

document.getElementById('resendBtn').addEventListener('click', function () {
    const resendButton = document.getElementById('resendBtn');
    resendButton.disabled = true;
    setStatus('Sending a new OTP…');

    postJson('/resend-otp', {})
        .then(function (data) {
            if (!data) {
                return;
            }
            if (data._httpStatus >= 400) {
                throw new Error(data.error || data.message || 'Unable to resend OTP.');
            }
            setStatus('A new OTP was sent to your registered phone.', 'success');
        })
        .catch(function (error) {
            setStatus(error.message, 'error');
        })
        .finally(function () {
            resendButton.disabled = false;
        });
});
