document.getElementById('verifyBtn').addEventListener('click', function() {
    // Get the user-entered OTP from the input field
    const userEnteredOTP = document.getElementById('otp').value;

    // Example: Sending the OTP to the server for verification
    // Note: This is a placeholder. You'll need to replace it with an actual AJAX request or fetch API call to your server.
    console.log('Verifying OTP:', userEnteredOTP);
    // Pretend we're sending the OTP to a server endpoint '/verify-otp'
    fetch('/verify-otp', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify({ otp: userEnteredOTP }),
    })
    .then(response => response.json())
    .then(data => {
        if(data.success) {
            // OTP verification succeeded
            alert('OTP verified successfully! You are now logged in.');
            // Redirect the user to the dashboard or another page as necessary
            window.location.href = '/dashboard';
        } else {
            // OTP verification failed
            alert('Incorrect OTP. Please try again.');
            // Optionally, clear the OTP field or take other actions in response to the failed verification
        }
    })
    .catch(error => {
        console.error('Error verifying OTP:', error);
        alert('An error occurred while verifying the OTP. Please try again.');
    });
});
