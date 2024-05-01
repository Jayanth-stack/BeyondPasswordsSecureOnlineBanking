//prevent browser back

history.pushState(null, null, document.URL);
window.addEventListener('popstate', function () {
    history.pushState(null, null, document.URL);
});

//prevent right-click
document.addEventListener("contextmenu", function(e){
  e.preventDefault();
})

window.onload = function() {
  if (localStorage.getItem('loggedStatus') == '0'){
    window.alert("You are not logged in!");
  }
  localStorage.removeItem('loggedStatus');
}

const homeURL = 'http://127.0.0.1:5000/';

var auth_user_type, forgotpw_userid_data;
const sign_in_btn = document.querySelector("#sign-in-btn");
const sign_up_btn = document.querySelector("#sign-up-btn");
const next_button_1 = document.querySelector("#next_btn_1");
const prev_button_1 = document.querySelector("#prev_btn_1");
const next_button_2 = document.querySelector("#next_btn_2");
const prev_button_2 = document.querySelector("#prev_btn_2");
// const next_button_3 = document.querySelector("#next_btn_3");
// const prev_button_3 = document.querySelector("#prev_btn_3");
const forgot_pw_link = document.querySelector("#forgot_pw_link");
const verify_btn = document.querySelector("#verify_btn");
const reset_pw_btn = document.querySelector("#reset_pw_btn");
const reset_pw_1 = document.querySelector("#reset_pw_1");
const reset_pw_2 = document.querySelector("#reset_pw_2");
const checkmark = document.querySelector("#checkmark");
const checkmark_circle = document.querySelector("#checkmark_circle");
const checkmark_check = document.querySelector("#checkmark_check");
const container = document.querySelector(".container");
var otpsend = document.querySelector("#otpsend");
var otptimer = document.querySelector("#otptimer");
const otp_verify_btn = document.querySelector("#otp_verify_btn");
const virtual_keyboard_button = document.querySelector("#virtual_keyboard");
const key_1 = document.querySelector("#key_1");
const key_2 = document.querySelector("#key_2");
const key_3 = document.querySelector("#key_3");
const key_4 = document.querySelector("#key_4");
const key_5 = document.querySelector("#key_5");
const key_6 = document.querySelector("#key_6");
const key_7 = document.querySelector("#key_7");
const key_8 = document.querySelector("#key_8");
const key_9 = document.querySelector("#key_9");
const key_0 = document.querySelector("#key_0");
const key_backs = document.querySelector("#key_backs");
const key_ac = document.querySelector("#key_ac");

key_1.addEventListener("click", () => {
  document.querySelector("#otp_verify_input").value += '1';
});
key_2.addEventListener("click", () => {
  document.querySelector("#otp_verify_input").value += '2';
});
key_3.addEventListener("click", () => {
  document.querySelector("#otp_verify_input").value += '3';
});
key_4.addEventListener("click", () => {
  document.querySelector("#otp_verify_input").value += '4';
});
key_5.addEventListener("click", () => {
  document.querySelector("#otp_verify_input").value += '5';
});
key_6.addEventListener("click", () => {
  document.querySelector("#otp_verify_input").value += '6';
});
key_7.addEventListener("click", () => {
  document.querySelector("#otp_verify_input").value += '7';
});
key_8.addEventListener("click", () => {
  document.querySelector("#otp_verify_input").value += '8';
});
key_9.addEventListener("click", () => {
  document.querySelector("#otp_verify_input").value += '9';
});
key_0.addEventListener("click", () => {
  document.querySelector("#otp_verify_input").value += '0';
});
key_backs.addEventListener("click", () => {
  document.querySelector("#otp_verify_input").value = document.querySelector("#otp_verify_input").value.slice(0, -1);
});
key_ac.addEventListener("click", () => {
  document.querySelector("#otp_verify_input").value = '';
});

virtual_keyboard_button.addEventListener("click", () => {
  if (document.querySelector("#keyboard").style.display == 'none') {
    document.querySelector("#keyboard").style.display = 'block';
  }
  else {
    document.querySelector("#keyboard").style.display = 'none';
  }
});

sign_up_btn.addEventListener("click", () => {
  container.classList.add("sign-up-mode");
  document.querySelector(".sign-in-form").reset();
});

forgot_pw_link.addEventListener("click", () => {
  if (document.querySelector("#keyboard").style.display == 'block') {
    document.querySelector("#keyboard").style.display = 'none';
  }
  document.querySelector("#otp_verify_input").value = '';
  container.classList.add("forgot-pw-mode");
});

otp_verify_btn.addEventListener("click", () => {
  console.log("verifyOTP called");

  const otpData = {
    userid : forgotpw_userid_data,
    otp : document.querySelector("#otp_verify_input").value,
    requester : auth_user_type
  };

  fetch(homeURL+'OTPAccess', {
    method : 'post',
    body : JSON.stringify(otpData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("verifyOTP response received");
    return response.json();
  }).then(function(data) {
    console.log(data);
    if(data.message == 'verified') {
      container.classList.add("reset-pw-mode");
      if (checkmark_circle.classList.contains("checkmark__circle")){
        checkmark_circle.classList.remove("checkmark__circle");
        checkmark_check.classList.remove("checkmark__check");
        reset_pw_btn.removeAttribute('disabled');
        reset_pw_1.removeAttribute('disabled');
        reset_pw_2.removeAttribute('disabled');
      }
    }
    else {
      window.alert("OTP mismatched!");
    }
  }).catch(function(error){
    console.error(error);
  })
});
const forgot_pw_form = document.querySelector(".forgot-pw-form");
forgot_pw_form.addEventListener("submit", function(e) {
  e.preventDefault();
  console.log("forgotpw called");
  otpsend.setAttribute('value','Re-send OTP');
  startTimer(60, otptimer);
  forgotpw_userid_data = document.querySelector("#forgotpw_userid").value;
  auth_user_type = document.querySelector(".auth_usertype_check").value;

  const forgotpwData = {
    userid : forgotpw_userid_data,
    requester : auth_user_type
  };

  fetch(homeURL+'sendOTP', {
    method : 'post',
    body : JSON.stringify(forgotpwData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("forgotpw response received");
    return response.json();
  }).then(function(data) {
    if(data.message == 'OTP Sent') {
      window.alert('An OTP has been sent to your registered mobile phone no.');
    }
    else {
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
  })
});

sign_in_btn.addEventListener("click", () => {
  if (container.classList.contains("forgot-pw-mode")){
    if (container.classList.contains("reset-pw-mode")){
      container.classList.remove("reset-pw-mode");
      document.querySelector(".reset-pw-form").reset();
      if (checkmark.getAttribute('visibility') == "visible"){
        checkmark.setAttribute('visibility','hidden');
      }
    }
    container.classList.remove("forgot-pw-mode");
    document.querySelector(".forgot-pw-form").reset();
  }
  else{
    // if (container.classList.contains("security-questions-mode"))
    //   container.classList.remove("security-questions-mode");
    if (container.classList.contains("address-info-mode"))
      container.classList.remove("address-info-mode");
    if (container.classList.contains("user-details-mode"))
      container.classList.remove("user-details-mode");
    container.classList.remove("sign-up-mode");
  }
});

// next_button_1.addEventListener("click", () => {
//   container.classList.add("user-details-mode");
// });
const sign_up_form_1 = document.querySelector("#sign_up_form_1");
sign_up_form_1.addEventListener("submit", function(e) {
  e.preventDefault();
  if(document.querySelector('#agree_checkbox').checked == false){
    window.alert('You must agree to our Terms and Conditions to move forward.');
  }
  else{
    if(document.querySelector('#register_password').value == document.querySelector('#register_confirm_password').value) {
      container.classList.add("user-details-mode");
    }
    else {
      window.alert("Password mismatch!");
    }
  }
});

prev_button_1.addEventListener("click", () => {
  container.classList.remove("user-details-mode");
});

// next_button_2.addEventListener("click", () => {
//   container.classList.add("address-info-mode");
// });
const sign_up_form_2 = document.querySelector("#sign_up_form_2");
sign_up_form_2.addEventListener("submit", function(e) {
  e.preventDefault();
  container.classList.add("address-info-mode");
});

prev_button_2.addEventListener("click", () => {
  container.classList.remove("address-info-mode");
});

// next_button_3.addEventListener("click", () => {
//   container.classList.add("security-questions-mode");
// });
const sign_up_form_3 = document.querySelector("#sign_up_form_3");
sign_up_form_3.addEventListener("submit", function(e) {
  e.preventDefault();
  console.log("register called");

  const register_userid_data = document.querySelector("#register_userid").value;
  const register_password_data = document.querySelector("#register_password").value;
  const register_email_data = document.querySelector("#register_email").value;
  const register_fname_data = document.querySelector("#register_fname").value;
  const register_mname_data = document.querySelector("#register_mname").value;
  const register_lname_data = document.querySelector("#register_lname").value;
  const register_phone_data = document.querySelector("#register_phone").value;
  const register_dob_data = document.querySelector("#register_dob").value;
  const register_ssn_data = document.querySelector("#register_ssn").value;
  const register_address1_data = document.querySelector("#register_address1").value;
  const register_address2_data = document.querySelector("#register_address2").value;
  const register_state_data = document.querySelector("#register_state").value;
  const register_zip_data = document.querySelector("#register_zip").value;

  localStorage.setItem('user', register_userid_data);
  console.log("register userid stored in local data = "+ localStorage.getItem('user'));

  const registerData = {
    empid : 'None',
    userid : register_userid_data,
    password : register_password_data,
    email : register_email_data,
    firstname : register_fname_data,
    midname : register_mname_data,
    lastname : register_lname_data,
    phone : register_phone_data,
    dob : register_dob_data,
    ssn : register_ssn_data,
    address : register_address1_data + '\n' + register_address2_data + '\n' + register_state_data + '\n' + register_zip_data
  };

  fetch(homeURL+'registerCustomer', {
    method : 'post',
    body : JSON.stringify(registerData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("register response received");
    if (response.redirected) {
      sign_up_form_1.reset();
      sign_up_form_2.reset();
      sign_up_form_3.reset();
      window.location.href = response.url;
    }
    else {
      window.alert("Oops, We encountered an error, Please re-try again later :)");
    }
  }).catch(function(error){
    console.error(error);
  })
});

// prev_button_3.addEventListener("click", () => {
//   container.classList.remove("security-questions-mode");
// });

// const sign_up_form_4 = document.querySelector("#sign_up_form_4");
// sign_up_form_4.addEventListener("submit", function(e) {
//   e.preventDefault();
// });


// otpsend.addEventListener("click", () => {
//   otpsend.setAttribute('value','Re-send OTP');
//   startTimer(60, otptimer);
// });

function startTimer(duration, display) {
  otptimer.style.visibility = 'visible';
  otpsend.disabled = 'true';
  var timer = duration;
  var timerId = setInterval(function () {
      display.textContent = timer;

      if (--timer < 0) {
          otptimer.style.visibility = 'hidden';
          otpsend.removeAttribute('disabled');
          clearInterval(timerId);
          display.textContent = duration;
      }
  }, 1000);
}

// reset_pw_btn.addEventListener("click", () => {
//   checkmark.setAttribute('visibility','visible');
//   checkmark_circle.classList.add("checkmark__circle");
//   checkmark_check.classList.add("checkmark__check");
//   reset_pw_1.disabled = 'true';
//   reset_pw_2.disabled = 'true';
//   reset_pw_btn.disabled = 'true';
// });
const reset_pw_form = document.querySelector(".reset-pw-form");
reset_pw_form.addEventListener("submit", function(e) {
  e.preventDefault();
  console.log("resetpw called");

  const resetpw_pw_data = reset_pw_1.value;
  const resetpw_confirm_data = reset_pw_2.value;

  if(resetpw_pw_data == resetpw_confirm_data) {
    const resetpwData = {
      userid : forgotpw_userid_data,
      oldPassword : '',
      newPassword : resetpw_pw_data,
      requester : auth_user_type,
      flag : 0
    };

    fetch(homeURL+'resetPassword', {
      method : 'post',
      body : JSON.stringify(resetpwData),
      headers : {
        'Content-type' : 'application/json'
      }
    }).then(function(response) {
      console.log("resetpw response received");
      return response.json();
    }).then(function (data) {
      console.log(data);
      if (data.message == "Password Updated") {
        checkmark.setAttribute('visibility','visible');
        checkmark_circle.classList.add("checkmark__circle");
        checkmark_check.classList.add("checkmark__check");
        reset_pw_1.disabled = 'true';
        reset_pw_2.disabled = 'true';
        reset_pw_btn.disabled = 'true';
      }
      else {
        window.alert("Oops, We encountered an error, Please re-try again later :)");
      }
    }).catch(function(error){
      console.error(error);
    })
  }
  else {
    window.alert("Password mismatch!");
  }
});

const sign_in_form = document.querySelector(".sign-in-form");
sign_in_form.addEventListener('submit', function(e) {
  e.preventDefault();
  console.log("login called");

  if(document.querySelector("#honey_input").value != ''){
    window.alert('Are you a Robot? Please fill-up the required fields manually.');
    document.querySelector("#userid_input").value = '';
    document.querySelector("#password_input").value = '';
    document.querySelector("#honey_input").value = '';
  }
  else {
    const userid_data = document.querySelector("#userid_input").value;
    const password_data = document.querySelector("#password_input").value;
    const usertype_data = document.querySelector(".employee_login_check").value;

    localStorage.setItem('user', userid_data);
    console.log("login userid stored in local data = "+ localStorage.getItem('user'));
    localStorage.setItem('usertype', usertype_data);
    console.log("login usertype stored in local data = "+ localStorage.getItem('usertype'));

    const loginData = {
      userid : userid_data,
      password : password_data,
      usertype : usertype_data
    };
    fetch(homeURL+'login', {
      method : 'post',
      body : JSON.stringify(loginData),
      headers : {
        'Content-type' : 'application/json'
      }
    }).then(function(response) {
      console.log("login response received");
      if (response.redirected) {
        window.location.href = response.url;
      }
      else {
        window.alert("Invalid credentials!");
      }
    }).catch(function(error){
      console.error(error);
    });
  }
});




