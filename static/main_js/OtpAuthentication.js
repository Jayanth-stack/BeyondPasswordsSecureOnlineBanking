document.getElementById('verifyBtn').addEventListener('click', function() {
    const userEnteredOTP = document.getElementById('otp').value;
    const homeURL = 'http://127.0.0.1:5000/';
    const otpInputField = document.getElementById('otp');
    const verifyButton = document.getElementById('verifyBtn');

    if (userEnteredOTP.trim() === '') {
        alert('Please enter the OTP.');
        return;
    }

    verifyButton.textContent = 'Verifying...';
    verifyButton.disabled = true;

    console.log('Verifying OTP:', userEnteredOTP);
    fetch(homeURL + 'verify-otp', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ otp_code: userEnteredOTP }),
    })
    .then(response => {
        console.log("OTP verification response received");
        if (response.redirected) {
            window.location.href = response.url;
        } else {
            return response.json();
        }
    })
    .then(data => {
        if (data && !data.success) {
            throw new Error(data.error || 'Incorrect OTP. Please try again.');
        }
    })
    .catch(error => {
        console.error('Error verifying OTP:', error);
        alert('Error: ' + error.message);
    })
    .finally(() => {
        verifyButton.textContent = 'Verify OTP';
        verifyButton.disabled = false;
    });
});
