//prevent browser back
history.pushState(null, null, document.URL);
window.addEventListener('popstate', function () {
    history.pushState(null, null, document.URL);
});

//prevent right-click
document.addEventListener("contextmenu", function(e){
  e.preventDefault();
})

const homeURL = 'https://www.cse545group6.online/';

// var data = {
//   "Accounts" : {
//     "checkin" : "None",
//     "credit" : {
//       "Account" : 2,
//       "Balance" : 0
//     },
//     "savings" : {
//       "Account" : 3,
//       "Balance" : 500
//     }
//   },
//   "Info" : [
//     [
//       "rohitkh","khadwal","","ROHIT","Sun, 21 MAR 2021 00:00:00 GMT","8894141786","anil.khadwal@gmail.com","","","ssn",1,"Sun, 21 MAR 2021 00:00:00 GMT","sdk"
//     ]
//   ]
// };

var userid, usertype, first_name, savings_ac_no, savings_ac_no_masked, savings_ac_bal, checking_ac_no, checking_ac_no_masked, checking_ac_bal, cc_no, cc_no_masked, cc_bal;
var midname, lastname, email, contact, dob, ssn, ssn_masked, address;
var otpModal_source;

const sa_card = document.querySelector("#sa_card");
const ca_card = document.querySelector("#ca_card");
const cc_card = document.querySelector("#cc_card");

function getUser() {
  console.log("getuser called");

  //userid = localStorage.getItem('user');
  console.log("userid retrieved from local storage ="+ userid);

  const loadUserData = {
    customer_id : userid,
    usertype : usertype
  };

  fetch(homeURL+'loadCustomer', {
    method : 'post',
    body : JSON.stringify(loadUserData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("getuser response received");
    if (response.redirected) {
      localStorage.setItem('loggedStatus', '0');
      window.location.href = response.url;
    }
    else {
      return response.json();
    }
  }).then(function (data) {
    console.log(data);
    appendPrimaryData(data);
  }).catch(function(error){
    console.error(error);
  });
}

function appendPrimaryData(data) {
  first_name = data.Info.first_name;
  if(data.Accounts.savings == "None"){
    //sa_card.style.visibility = 'hidden';
    $('#sa_card').hide();
  }
  else {
    //sa_card.style.visibility = 'visible';
    $('#sa_card').show();
  }
  if(data.Accounts.checkin == "None"){
    //ca_card.style.visibility = 'hidden';
    $('#ca_card').hide();
  }
  else {
    //ca_card.style.visibility = 'visible';
    $('#ca_card').show();
  }
  if(data.Accounts.credit == "None"){
    //cc_card.style.visibility = 'hidden';
    $('#cc_card').hide();
  }
  else {
    //sa_card.style.visibility = 'visible';
    $('#cc_card').show();
  }

  if($('#sa_card').css('display')=='none' && $('#ca_card').css('display')=='none' && $('#cc_card').css('display')=='none'){
    console.log('none');
    if($('#no_accounts_pane').css('display')=='none'){
      $('#no_accounts_pane').show().siblings('div').hide();
    }
  }

  savings_ac_no = data.Accounts.savings.Account;
  //savings_ac_no_masked = '******'+savings_ac_no.slice(-4);
  savings_ac_bal = data.Accounts.savings.Balance;
  checking_ac_no = data.Accounts.checkin.Account;
  //checking_ac_no_masked = '******'+checking_ac_no.slice(-4);
  checking_ac_bal = data.Accounts.checkin.Balance;
  cc_no = data.Accounts.credit.Account;
  //cc_no_masked = '******'+cc_no.slice(-4);
  cc_bal = data.Accounts.credit.Balance;

  midname = data.Info.middle_name;
  lastname = data.Info.last_name;
  email = data.Info.email_id;
  contact = data.Info.contact_no;
  dob = data.Info.dob;
  ssn = data.Info.ssn;
  ssn_masked = '***-**-'+ssn.slice(-4);
  address = data.Info.address;

  document.getElementById("user_name").innerHTML = first_name + '!';
  document.getElementById("sa_no").innerHTML = savings_ac_no;
  document.getElementById("sa_balance").innerHTML = savings_ac_bal;
  document.getElementById("ca_no").innerHTML = checking_ac_no;
  document.getElementById("ca_balance").innerHTML = checking_ac_bal;
  document.getElementById("cc_no").innerHTML = cc_no;
  document.getElementById("cc_balance").innerHTML = cc_bal;

  document.getElementById("first_name").value = first_name;
  document.getElementById("middle_name").value = midname;
  document.getElementById("last_name").value = lastname;
  document.getElementById("email_id").value = email;
  document.getElementById("contact_no").value = contact;
  document.getElementById("dob").value = dob;
  document.getElementById("ssn").value = ssn_masked;
  document.getElementById("address").value = address;

  createCheckDropdown();
  fillPendingTransTbl(data);
}

function fillPendingTransTbl(data){
  var table = document.getElementById('pending_requests_tbl');
  var rowCount = table.rows.length;

  var selection = document.getElementById('xact_id_acc_den');
  try {
    for(var i=1; i<rowCount; i++) {
      table.deleteRow(i);
      selection.remove(i);
      rowCount--;
      i--;
    }
  }catch(e) {
    alert(e);
  }
  if(data.FundsRequests != 'None') {
    for (var i=0; i< data.FundsRequests.length; i++){
      var rowCount = table.rows.length;
      var row = table.insertRow(rowCount);
      var cell1 = row.insertCell(0);
      cell1.innerHTML = data.FundsRequests[i][0];

      var cell2 = row.insertCell(1);
      cell2.innerHTML = data.FundsRequests[i][1];

      var cell3 = row.insertCell(2);
      cell3.innerHTML = data.FundsRequests[i][2];

      var cell4 = row.insertCell(3);
      cell4.innerHTML = data.FundsRequests[i][5];

      var option = document.createElement("OPTION");
      option.innerHTML = data.FundsRequests[i][0];
      option.value = data.FundsRequests[i][0];
      //Add the Option element to DropDownList.
      selection.options.add(option);
    }
  }
}

function createCheckDropdown() {
  if($('#sa_card').css('display')!='none' && !optionExists(savings_ac_no, document.querySelector('#orderCheck_fromAccount'))){
    var option = document.createElement("OPTION");
    option.innerHTML = 'Savings Account - '+savings_ac_no;
    option.value = savings_ac_no;
    //Add the Option element to DropDownList.
    document.querySelector('#orderCheck_fromAccount').options.add(option);
  }
  if($('#ca_card').css('display')!='none' && !optionExists(checking_ac_no, document.querySelector('#orderCheck_fromAccount'))){
    var option = document.createElement("OPTION");
    option.innerHTML = 'Checking Account - '+checking_ac_no;
    option.value = checking_ac_no;
    //Add the Option element to DropDownList.
    document.querySelector('#orderCheck_fromAccount').options.add(option);
  }
  if($('#cc_card').css('display')!='none' && !optionExists(cc_no, document.querySelector('#orderCheck_fromAccount'))){
    var option = document.createElement("OPTION");
    option.innerHTML = 'Credit Card - '+cc_no;
    option.value = cc_no;
    //Add the Option element to DropDownList.
    document.querySelector('#orderCheck_fromAccount').options.add(option);
  }
}

function optionExists(needle, haystack) {
  var optionExists = false,
      optionsLength = haystack.length;
  while(optionsLength--){
    if(haystack.options[optionsLength].value == needle){
      optionExists = true;
      break;
    }
  }
  return optionExists;
}

function logout() {
  console.log("logout called");

  //userid = localStorage.getItem('user');
  console.log("userid retrieved from local storage ="+ userid);

  const logoutUserData = {
    userid : userid
  };

  fetch(homeURL+'logout', {
    method : 'post',
    body : JSON.stringify(logoutUserData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("logout response received");
    if (response.redirected) {
      window.location.href = response.url;
    }
    else {
      window.alert("Oops we encountered an error!");
    }
  }).catch(function(error){
    console.error(error);
  });
}

function send_otp(){
  console.log("send_otp called");

  const sendotpData = {
    userid : userid,
    requester : 'Customer'
  };

  fetch(homeURL+'sendOTP', {
    method : 'post',
    body : JSON.stringify(sendotpData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("send_otp response received");
    return response.json();
  }).then(function(data) {
    if(data.message == 'OTP Sent'){
      if(otpModal_source == 'saTransfer'){
        $('#saTransfer_close').click();
        $('#otpModal').modal({backdrop: 'static', keyboard: false});
        $('#otpModal').modal('show');
      }
      else if(otpModal_source == 'caTransfer'){
        $('#caTransfer_close').click();
        $('#otpModal').modal({backdrop: 'static', keyboard: false});
        $('#otpModal').modal('show');
      }
      else if(otpModal_source == 'ccTransfer'){
        $('#ccTransfer_close').click();
        $('#otpModal').modal({backdrop: 'static', keyboard: false});
        $('#otpModal').modal('show');
      }
      else if(otpModal_source == 'approveTransfer'){
        $('#otpModal').modal({backdrop: 'static', keyboard: false});
        $('#otpModal').modal('show');
      }
      else {
        window.alert("OOPS! We encountered an error!");
      }
    }
    else {
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
  })
}

function verify_otp(otp){
  console.log("verify_otp called");

  const otpData = {
    userid : userid,
    otp : otp,
    requester : 'Customer'
  };

  fetch(homeURL+'verifyOTP', {
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
      if(otpModal_source == 'saTransfer'){
        $('#otpModal_close').click();
        $('#otpModal_input').val('');
        fund_transfer(userid, savings_ac_no, $('#sa_transfer_acno_input').val(), $('#saTransfer_amt').val());
      }
      else if(otpModal_source == 'caTransfer'){
        $('#otpModal_close').click();
        $('#otpModal_input').val('');
        fund_transfer(userid, checking_ac_no, $('#ca_transfer_acno_input').val(), $('#caTransfer_amt').val());
      }
      else if(otpModal_source == 'ccTransfer'){
        $('#otpModal_close').click();
        $('#otpModal_input').val('');
        fund_transfer(userid, cc_no, $('#cc_transfer_acno_input').val(), $('#ccTransfer_amt').val());
      }
      else if(otpModal_source == 'approveTransfer'){
        $('#otpModal_close').click();
        $('#otpModal_input').val('');
        approve_request(userid, $('#xact_id_acc_den').val());
      }
      else {
        window.alert("OOPS! We encountered an error!");
      }
    }
    else {
      window.alert("OTP mismatched!");
    }
  }).catch(function(error){
    console.error(error);
  })
}

function deposit(userid, account, amount) {
  console.log("deposit called");

  const depositData = {
    userid : userid,
    account : account,
    amount : amount
  };

  fetch(homeURL+'depositAmount', {
    method : 'post',
    body : JSON.stringify(depositData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("deposit response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Success') {
      window.alert("Amount credited!");
    }
    else {
      window.alert(data.message);
    }
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function fund_transfer(userid, fromAccount, toAccount, amount) {
  console.log("fund transfer called");

  const fundTransferData = {
    userid : userid,
    fromAccount : fromAccount,
    toAccount : toAccount,
    amount : amount
  };

  fetch(homeURL+'fundTransfer', {
    method : 'post',
    body : JSON.stringify(fundTransferData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("fundTransfer response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'done'){
      window.alert('Successfully transferred!');
    }
    else {
      window.alert(data.message);
    }
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function fund_request(userid, toAccount, fromAccount, amount) {
  console.log("fund request called");

  const fundRequestData = {
    userid : userid,
    fromAccount : fromAccount,
    toAccount : toAccount,
    amount : amount
  };

  fetch(homeURL+'requestFunds', {
    method : 'post',
    body : JSON.stringify(fundRequestData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("fundRequest response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Request Sent'){
      window.alert('Request Sent!');
    }
    else {
      window.alert(data.message);
    }
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function approve_request(userid, xactno) {
  console.log("approve request called");

  const approveRequestData = {
    customer_id : userid,
    transaction_no : xactno
  };

  fetch(homeURL+'approveRequest', {
    method : 'post',
    body : JSON.stringify(approveRequestData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("approveRequest response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Invalid transaction_no'){
      window.alert('Invalid Transaction No input!');
    }
    else if(data.message == 'done'){
      window.alert('Approved!');
    }
    else{
      window.alert(data.message);
    }
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function deny_request(userid, xactno) {
  console.log("deny request called");

  const denyRequestData = {
    userid : userid,
    transaction_no : xactno
  };

  fetch(homeURL+'denyRequest', {
    method : 'post',
    body : JSON.stringify(denyRequestData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("denyRequest response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Invalid transaction_no'){
      window.alert('Invalid Transaction No input!');
    }
    else if(data.message == 'Request Cancelled'){
      window.alert('Denied!');
    }
    else{
      window.alert(data.message);
    }
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function getSavingXacts(userid, acno) {
  console.log("savings transactions called");

  const savingXactsData = {
    userid : userid,
    account_no : acno
  };

  fetch(homeURL+'getTransactionHistory', {
    method : 'post',
    body : JSON.stringify(savingXactsData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("savingXacts response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message[0][0] != ''){
      document.getElementById('sa_trans_tbl').innerHTML = data.message[0][0];
    }
  }).catch(function(error){
    console.error(error);
  });
}

function getCheckingXacts(userid, acno) {
  console.log("checking transactions called");

  const checkingXactsData = {
    userid : userid,
    account_no : acno
  };

  fetch(homeURL+'getTransactionHistory', {
    method : 'post',
    body : JSON.stringify(checkingXactsData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("checkingXacts response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    document.getElementById('ca_trans_tbl').innerHTML = data.message[0][0];
  }).catch(function(error){
    console.error(error);
  });
}

function getCCXacts(userid, acno) {
  console.log("savings transactions called");

  const ccXactsData = {
    userid : userid,
    account_no : acno
  };

  fetch(homeURL+'getTransactionHistory', {
    method : 'post',
    body : JSON.stringify(ccXactsData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("ccXacts response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    document.getElementById('cc_trans_tbl').innerHTML = data.message[0][0];
  }).catch(function(error){
    console.error(error);
  });
}

function order_check(userid, toAccount, fromAccount, amount) {
  console.log("getcashiercheck called");

  const orderCheckData = {
    userid : userid,
    to_account : toAccount,
    from_account : fromAccount,
    amount : amount
  };

  fetch(homeURL+'getCashierCheque', {
    method : 'post',
    body : JSON.stringify(orderCheckData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("orderCheck response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Success') {
      window.alert('Cheque Ordered!');
    }
    else {
      window.alert('Failed! Please re-try.');
    }
  }).catch(function(error){
    console.error(error);
  });
}

function dep_check(userid, checkno) {
  console.log("depositcheck called");

  const depCheckData = {
    userid : userid,
    cheque_no : checkno
  };

  fetch(homeURL+'depositCheck', {
    method : 'post',
    body : JSON.stringify(depCheckData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("depCheck response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Check already used'){
      window.alert('Cheque already used!');
    }
    if(data.message == 'Success') {
      window.alert('Cheque successfully deposited!');
    }
    else {
      window.alert('Failed! Please re-try.');
    }
  }).catch(function(error){
    console.error(error);
  });
}

function getchecks(userid) {
  console.log("getchecks called");

  const getChecksData = {
    userid : userid
  };

  fetch(homeURL+'getChequeList', {
    method : 'post',
    body : JSON.stringify(getChecksData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("getChecks response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message != 'None' || data.message != null){
      var checkData = '';
      for (var i=0; i< data.message.length; i++){
        checkData = checkData + 'Cheque No. = ' + data.message[i][0] +
                    ', Recipient Account = ' + data.message[i][1] +
                    ', from Account = ' + data.message[i][2] +
                    ', Amount = $' + data.message[i][3] +
                    ', Status = ' + (data.message[i][4] == 0 ? 'Deposited' : 'Active') + '<br />';
      }
      document.getElementById('view_check_tbl').innerHTML = checkData;
    }
  }).catch(function(error){
    console.error(error);
  });
}

function schedAppoint(userid, time) {
  console.log("schedAppoint called");

  const schedAppointData = {
    customer_id : userid,
    time : time
  };

  fetch(homeURL+'makeAppointment', {
    method : 'post',
    body : JSON.stringify(schedAppointData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("schedAppoint response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Appointment fixed'){
      window.alert('Appointment scheduled!');
    }
    else {
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
  });
}

function getAppoint(userid) {
  console.log("getAppoint called");

  const getAppointData = {
    customer_id : userid
  };

  fetch(homeURL+'getAppointmentList', {
    method : 'post',
    body : JSON.stringify(getAppointData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("getAppoint response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message != 'None' || data.message != null){
      var appointmentData = '';
      for (var i=0; i<data.message.length; i++){
        appointmentData = appointmentData + data.message[i][2] + '<br />';
      }
      document.getElementById('view_appoint_tbl').innerHTML = appointmentData;
    }
  }).catch(function(error){
    console.error(error);
  });
}

function updateInfo(userid, email_info, contact_info, address_info) {
  console.log("updateInfo called");

  const updateInfoData = {
    userid : userid,
    email : email_info,
    contact_no : contact_info,
    address : address_info,
    requester : 'Customer'
  };

  fetch(homeURL+'updateInfo', {
    method : 'post',
    body : JSON.stringify(updateInfoData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("updateInfo response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Update Info Request Placed'){
      window.alert('Update Info Request Placed!');
    }
    else {
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
  });
}

function newAcc(userid, account_type) {
  console.log("newAcc called");

  const newAccData = {
    userid : userid,
    customer_id : userid,
    account_type : account_type
  };

  fetch(homeURL+'openNewAccount', {
    method : 'post',
    body : JSON.stringify(newAccData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("newAcc response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message[0] == 'Customer already have '){
      window.alert('You already have a '+ data.message[1] + ' account!');
    }
    else if(data.message[0] == 'Done'){
      window.alert('Congratulations! You have a new account with us.');
    }
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function changePassword(userid, oldPassword, newPassword) {
  const resetpwData = {
    userid : userid,
    oldPassword : oldPassword,
    newPassword : newPassword,
    requester : 'Customer',
    flag : 1
  };

  fetch(homeURL+'resetpPassword', {
    method : 'post',
    body : JSON.stringify(resetpwData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("resetpw response received");
    if(response.redirected){
      localStorage.setItem('loggedStatus', '0');
      window.location.href = response.url;
    }
    else {
      return response.json();
    }
  }).then(function (data) {
    window.alert(data.message);
  }).catch(function(error){
    console.error(error);
  });
}

$(document).ready(function() {
  userid = localStorage.getItem('user');
  usertype = localStorage.getItem('usertype');

  var specialElementHandlers = {
    "#editor" : function(element, renderer){
      return true;
    }
  }

    $('#logout_btn').on('click', function(){
      logout();
    });
    $('#account_details_btn').on('click', function(){
      if($('#account_details_pane').css('display')=='none'){
          $('#account_details_pane').show().siblings('div').hide();
      }
      $('#service_requests_menu').css('background-color','maroon');
      $('#my_accounts_menu').css('background-color','maroon');
      $('#pending_transaction_requests_menu').css('background-color','maroon');
    });
    $('#service_requests_menu').on('click', function(){
      if($('#service_requests_pane').css('display')=='none'){
          $('#service_requests_pane').show().siblings('div').hide();
      }
      $('#service_requests_menu').css('background-color','#FF6600');
      $('#my_accounts_menu').css('background-color','maroon');
      $('#pending_transaction_requests_menu').css('background-color','maroon');
    });
    $('#my_accounts_menu').on('click', function(){
      getUser();
      //appendPrimaryData(data);
      if($('#sa_card').css('display')=='none' && $('#ca_card').css('display')=='none' && $('#cc_card').css('display')=='none'){
        console.log('none');
        if($('#no_accounts_pane').css('display')=='none'){
          $('#no_accounts_pane').show().siblings('div').hide();
        }
      }
      else {
        if($('#my_accs_pane').css('display')=='none'){
          $('#my_accs_pane').show().siblings('div').hide();
        }
      }
      $('#my_accounts_menu').css('background-color','#FF6600');
      $('#service_requests_menu').css('background-color','maroon');
      $('#pending_transaction_requests_menu').css('background-color','maroon');
    });
    $('#pending_transaction_requests_menu').on('click', function(){
      if($('#pending_transaction_requests_pane').css('display')=='none'){
          $('#pending_transaction_requests_pane').show().siblings('div').hide();
      }
      $('#pending_transaction_requests_menu').css('background-color','#FF6600');
      $('#my_accounts_menu').css('background-color','maroon');
      $('#service_requests_menu').css('background-color','maroon');
    });
    $('#my_accounts_menu').click();
	  $(".loader-wrapper").delay( 1000 ).fadeOut("slow");
    $('input:radio[name="satransfer"]').change(
    function(){
        if ($(this).is(':checked') && $(this).val() == 'sa_send_radio') {
          $('#sa_transfer_acno_input').attr("placeholder", "To Account No.");
        }
        else {
          $('#sa_transfer_acno_input').attr("placeholder", "From Account No.");
        }
    });
    $('input:radio[name="catransfer"]').change(
    function(){
        if ($(this).is(':checked') && $(this).val() == 'ca_send_radio') {
          $('#ca_transfer_acno_input').attr("placeholder", "To Account No.");
        }
        else {
          $('#ca_transfer_acno_input').attr("placeholder", "From Account No.");
        }
    });
    var date = new Date();
    if(date.getHours() >= 17) {
      date.setDate(date.getDate()+1);
      date.setHours(10);
      date.setMinutes(00);
    }
    else if(date.getHours() >= 9) {
      date.setDate(date.getHours()+2);
    }
    else {
      date.setMinutes(00);
    }
    $('.date-picker-start').datepicker({
        format: 'dd.mm.yyyy',
        numberOfMonths: 2,
        autoclose : true,
        beforeShowDay: $.datepicker.noWeekends,
        minDate: date
    }).on('changeDate',function(e){
        //on change of date on start datepicker, set end datepicker's date
        $('.date-picker-end').datepicker('setStartDate',e.date)
    });
    $('.time-picker-start').timepicker({
    timeFormat: 'h:mm p',
    interval: 60,
    minTime: '10',
    maxTime: '6:00pm',
    defaultTime: '10',
    startTime: date,
    dynamic: false,
    dropdown: true,
    scrollbar: true,
    zindex: 9999
    });
    $('#saDeposit_btn').on('click', function(){
      console.log("deposit function");
      if($('#saDeposit_amt').val() == ''){
        alert('No amount detected!');
      }
      else {
        deposit(userid, savings_ac_no, $('#saDeposit_amt').val());
        $('#saDeposit_close').click();
      }
    });
    $('#caDeposit_btn').on('click', function(){
      console.log("deposit function");
      if($('#caDeposit_amt').val() == ''){
        alert('No amount detected!');
      }
      else {
        deposit(userid, checking_ac_no, $('#caDeposit_amt').val());
        $('#caDeposit_close').click();
      }
    });
    $('#saTransfer_btn').on('click', function(){
      console.log("transfer function");
      if($('#saTransfer_amt').val() == '' || $('#sa_transfer_acno_input').val() == ''){
        alert('Empty Field detected!');
      }
      else {
        if($('#sa_send_radio').is(':checked')) {
          if($('#saTransfer_amt').val() > savings_ac_bal){
            alert('You can not send more than you have silly!');
          }
          else {
            otpModal_source = 'saTransfer';
            send_otp();
            //fund_transfer(userid, savings_ac_no, $('#sa_transfer_acno_input').val(), $('#saTransfer_amt').val());
          }
        }
        else if($('#sa_request_radio').is(':checked')){
          $('#saTransfer_close').click();
          fund_request(userid, savings_ac_no, $('#sa_transfer_acno_input').val(), $('#saTransfer_amt').val());
        }
      }
    });
    $('#caTransfer_btn').on('click', function(){
      console.log("transfer function");
      if($('#caTransfer_amt').val() == '' || $('#ca_transfer_acno_input').val() == ''){
        alert('Empty Field detected!');
      }
      else {
        if($('#ca_send_radio').is(':checked')) {
          if($('#caTransfer_amt').val() > checking_ac_bal){
            alert('You can not send more than you have silly!');
          }
          else {
            otpModal_source = 'caTransfer';
            send_otp();
            //fund_transfer(userid, checking_ac_no, $('#ca_transfer_acno_input').val(), $('#caTransfer_amt').val());
          }
        }
        else if($('#ca_request_radio').is(':checked')){
          $('#caTransfer_close').click();
          fund_request(userid, checking_ac_no, $('#ca_transfer_acno_input').val(), $('#caTransfer_amt').val());
        }
      }
    });
    $('#ccTransfer_btn').on('click', function(){
      console.log("transfer function");
      if($('#ccTransfer_amt').val() == '' || $('#ccTransfer_account').val() == ''){
        alert('Empty Field detected!');
      }
      else {
        //fund_transfer(userid, cc_no, $('#ccTransfer_account').val(), $('#ccTransfer_amt').val());
        otpModal_source = 'ccTransfer';
        send_otp();
      }
    });
    $('#pending_req_accept').on('click', function(){
      console.log("accept request function");
      if($('#xact_id_acc_den').val() == 'select'){
        alert('No Transaction No. detected!');
      }
      else {
        //approve_request(userid, $('#xact_id_acc_den').val());
        otpModal_source = 'approveTransfer';
        send_otp();
      }
    });
    $('#pending_req_deny').on('click', function(){
      console.log("deny request function");
      if($('#xact_id_acc_den').val() == 'select'){
        alert('No Transaction ID detected!');
      }
      else {
        deny_request(userid, $('#xact_id_acc_den').val());
      }
    });
    $('#view_sa_xacts_btn').on('click', function(){
      console.log("savings transactions function");
      getSavingXacts(userid, savings_ac_no);
    });
    $('#view_ca_xacts_btn').on('click', function(){
      console.log("checking transactions function");
      getCheckingXacts(userid, checking_ac_no);
    });
    $('#view_cc_xacts_btn').on('click', function(){
      console.log("credit card transactions function");
      getCCXacts(userid, cc_no);
    });
    $('#orderCheck_btn').on('click', function(){
      console.log("getcashiercheck function");
      if($('#orderCheck_amt').val() == '' || $('#orderCheck_toAccount').val() == '' || $('#orderCheck_fromAccount').val() == 'select'){
        alert('Empty Field detected!');
      }
      else {
        order_check(userid, $('#orderCheck_toAccount').val(), $('#orderCheck_fromAccount').val(), $('#orderCheck_amt').val());
        $('#orderCheck_close').click();
      }
    });
    $('#depCheck_btn').on('click', function(){
      console.log("depcheck function");
      if($('#depCheck_no').val() == ''){
        alert('No Check No. detected!');
      }
      else {
        dep_check(userid, $('#depCheck_no').val());
        $('#depCheck_close').click();
      }
    });
    $('#viewCheck_btn').on('click', function(){
      console.log("getcheck function");
      getchecks(userid);
    });
    $('#schedApp_btn').on('click', function(){
      console.log("schedAppoint function");
      if($('.date-picker-start').val() == ''){
        alert('Date not detected!');
      }
      else{
        schedAppoint(userid, $('.date-picker-start').val()+', '+$('.time-picker-start').val());
        $('#schedApp_close').click();
      }
    });
    $('#viewAppoint_btn').on('click', function(){
      console.log("getAppoint function");
      getAppoint(userid);
    });
    $('#update_info_btn').on('click', function(){
      console.log("updateInfo function");
      updateInfo(userid, $('#email_id').val(), $('#contact_no').val(), $('#address').val());
    });
    $('#newAcc_btn').on('click', function(){
      console.log("newAcc function");
      newAcc(userid, $('#account_selection').val());
      $('#newAcc_close').click();
    });
    $('#home_logo').on('click', function(){
      $('#my_accounts_menu').click();
    });
    $('#saXact_dwnld_btn').on('click', function(){
      var doc = new jsPDF();
      doc.fromHTML($('#sa_trans_tbl')[0],15,15, {
        "elementHandlers" : specialElementHandlers
      });
      doc.save(savings_ac_no+'-Transactions.pdf');
    });
    $('#caXact_dwnld_btn').on('click', function(){
      var doc = new jsPDF();
      doc.fromHTML($('#ca_trans_tbl')[0],15,15, {
        "elementHandlers" : specialElementHandlers
      });
      doc.save(checking_ac_no+'-Transactions.pdf');
    });
    $('#ccXact_dwnld_btn').on('click', function(){
      var doc = new jsPDF();
      doc.fromHTML($('#cc_trans_tbl')[0],15,15, {
        "elementHandlers" : specialElementHandlers
      });
      doc.save(cc_no+'-Transactions.pdf');
    });
    $('#changePW_btn').on('click', function(){
      if($('#changePW_oldPW').val() == '' || $('#changePW_newPW').val() == '' || $('#changePW_confirmPW').val() == '') {
        window.alert("Empty Input!");
      }
      else {
        if($('#changePW_newPW').val() == $('#changePW_oldPW').val()){
          window.alert("Old & New Password shouldn't be the same!");
        }
        else {
          if($('#changePW_newPW').val() == $('#changePW_confirmPW').val()){
            changePassword(userid, $('#changePW_oldPW').val(), $('#changePW_newPW').val());
            $('#changePW_close').click();
          }
          else {
            window.alert("Re-entered password doesn't match new password!");
          }
        }
      }
    });
    $('#key_1').on('click', function(){
      $("#otpModal_input").val($("#otpModal_input").val() + '1');
    });
    $('#key_2').on('click', function(){
      $("#otpModal_input").val($("#otpModal_input").val() + '2');
    });
    $('#key_3').on('click', function(){
      $("#otpModal_input").val($("#otpModal_input").val() + '3');
    });
    $('#key_4').on('click', function(){
      $("#otpModal_input").val($("#otpModal_input").val() + '4');
    });
    $('#key_5').on('click', function(){
      $("#otpModal_input").val($("#otpModal_input").val() + '5');
    });
    $('#key_6').on('click', function(){
      $("#otpModal_input").val($("#otpModal_input").val() + '6');
    });
    $('#key_7').on('click', function(){
      $("#otpModal_input").val($("#otpModal_input").val() + '7');
    });
    $('#key_8').on('click', function(){
      $("#otpModal_input").val($("#otpModal_input").val() + '8');
    });
    $('#key_9').on('click', function(){
      $("#otpModal_input").val($("#otpModal_input").val() + '9');
    });
    $('#key_0').on('click', function(){
      $("#otpModal_input").val($("#otpModal_input").val() + '0');
    });
    $('#key_backs').on('click', function(){
      $("#otpModal_input").val($("#otpModal_input").val().slice(0, -1));
    });
    $('#key_ac').on('click', function(){
      $("#otpModal_input").val('');
    });
    $('#otpModal_btn').on('click', function(){
      if($('#otpModal_input').val() == ''){
        window.alert('Please Enter OTP');
      }
      else {
        verify_otp($('#otpModal_input').val());
      }
    });
});
