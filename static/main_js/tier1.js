//prevent browser back
history.pushState(null, null, document.URL);
window.addEventListener('popstate', function () {
    history.pushState(null, null, document.URL);
});

//prevent right-click
document.addEventListener("contextmenu", function(e){
  e.preventDefault();
})

const homeURL = 'http://127.0.0.1:5000/';

var userid, usertype, firstname, midname, lastname, email, contact, dob, ssn, address;

function getUser() {
  console.log("gettier1 called");

  //userid = localStorage.getItem('user');
  console.log("userid retrieved from local storage ="+ userid);

  const loadUserData = {
    employee_id : userid,
    usertype : 'tier1'
  };

  fetch(homeURL+'loadEmployee', {
    method : 'get',
    body : JSON.stringify(loadUserData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("gettier1 response received");
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
  midname = data.Info.middle_name;
  lastname = data.Info.last_name;
  email = data.Info.email_id;
  contact = data.Info.contact_no;
  dob = data.Info.dob;
  ssn = data.Info.ssn;
  address = data.Info.address;

  document.getElementById("account_first_name").value = first_name;
  document.getElementById("account_middle_name").value = midname;
  document.getElementById("account_last_name").value = lastname;
  document.getElementById("account_email_id").value = email;
  document.getElementById("account_contact_no").value = contact;
  document.getElementById("account_dob").value = dob;
  document.getElementById("account_ssn").value = ssn;
  document.getElementById("account_address").value = address;

  fillPendingReqTbl(data);
  fillPendingTransTbl(data);
}

function fillPendingTransTbl(data){
  var table = document.getElementById('pending_transactions_tbl');
  var rowCount = table.rows.length;

  var selection = document.getElementById('customer_trans_no');
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

function fillPendingReqTbl(data){
  var table = document.getElementById('cust_reqs_tbl');
  var rowCount = table.rows.length;

  var selection = document.getElementById('customer_req_id');
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

  if(data.UpdateInfo != 'None') {
    for (var i=0; i< data.UpdateInfo.length; i++){
      var rowCount = table.rows.length;
      var row = table.insertRow(rowCount);
      var cell1 = row.insertCell(0);
      cell1.innerHTML = data.UpdateInfo[i][0];

      var cell2 = row.insertCell(1);
      cell2.innerHTML = data.UpdateInfo[i][2];

      var cell3 = row.insertCell(2);
      cell3.innerHTML = 'Contact No : ' + data.UpdateInfo[i][3] + '<br>' +
      'Email-id : ' + data.UpdateInfo[i][4] + '<br>' +
      'Address : ' + data.UpdateInfo[i][5];

      var option = document.createElement("OPTION");
      option.innerHTML = data.UpdateInfo[i][0];
      option.value = data.UpdateInfo[i][0];
      //Add the Option element to DropDownList.
      selection.options.add(option);
    }
  }
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

function approve_request(userid, xactno) {
  console.log("approve request called");

  const approveRequestData = {
    userid : userid,
    transaction_no : xactno
  };

  fetch(homeURL+'approveRequestEmp', {
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

function updateInfo(userid, email_info, contact_info, address_info) {
  console.log("updateInfo called");

  const updateInfoData = {
    userid : userid,
    email : email_info,
    contact_no : contact_info,
    address : address_info,
    requester : 'Employee'
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

function getCustomer(customer_id) {
  console.log("getcustomer called");

  const loadCustomerData = {
    userid: userid,
    customer_id : customer_id
  };

  fetch(homeURL+'getCustomer', {
    method : 'post',
    body : JSON.stringify(loadCustomerData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("getcustomer response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    appendSecondaryData(customer_id, data);
  }).catch(function(error){
    console.error(error);
  });
}

function appendSecondaryData(customer_id, data) {

  document.getElementById("customer_id").innerHTML = customer_id;
  document.getElementById("first_name").innerHTML = data.Info.first_name;
  document.getElementById("middle_name").innerHTML = data.Info.middle_name;
  document.getElementById("last_name").innerHTML = data.Info.last_name;
  document.getElementById("email_id").innerHTML = data.Info.email_id;
  document.getElementById("contact_no").innerHTML = data.Info.contact_no;
  document.getElementById("dob").innerHTML = data.Info.dob;
  document.getElementById("ssn").innerHTML = data.Info.ssn;
  document.getElementById("address").innerHTML = data.Info.address;

  fillCustomerAccTbl(data);
  fillStaffLinked(data);
  fillStaffWires(data);
  fillStaffInWires(data);
  if($('#cust_details_card').css('display')=='none'){
    $('#cust_details_card').show();
  }
  if($('#cust_accounts_tbl').css('display')=='none'){
    $('#cust_details_tbl').show();
  }
}

function fillStaffLinked(data) {
  var snapshot = data.LinkedAccounts || {};
  var card = document.getElementById('staff_linked_card');
  if (!card) {
    return;
  }
  card.style.display = 'block';
  var summary = document.getElementById('staff_linked_summary');
  if (summary) {
    summary.innerHTML = 'YTD push: $' + (snapshot.ytd_push || '0.00') +
      ' &middot; YTD pull: $' + (snapshot.ytd_pull || '0.00') +
      ' &middot; returned: $' + (snapshot.returned_ytd || '0.00');
  }
  var table = document.getElementById('staff_linked_tbl');
  var body = table.getElementsByTagName('tbody')[0];
  body.innerHTML = '';
  var linkSelect = document.getElementById('staff_la_link_id');
  linkSelect.options.length = 1;
  var links = snapshot.links || [];
  for (var i = 0; i < links.length; i++) {
    var row = body.insertRow(-1);
    row.insertCell(0).innerHTML = links[i].nickname;
    row.insertCell(1).innerHTML = links[i].method;
    row.insertCell(2).innerHTML = links[i].status;
    row.insertCell(3).innerHTML = (links[i].routing_last4 || '') + '/' + (links[i].account_last4 || '');
    if (links[i].status != 'pending') {
      continue;
    }
    var option = document.createElement('OPTION');
    option.value = links[i].link_id;
    option.innerHTML = links[i].nickname + ' (' + links[i].method + ')';
    linkSelect.options.add(option);
  }
  var moveTable = document.getElementById('staff_linked_movements_tbl');
  var moveBody = moveTable.getElementsByTagName('tbody')[0];
  moveBody.innerHTML = '';
  var selection = document.getElementById('staff_la_movement_id');
  selection.options.length = 1;
  var movements = snapshot.movements || [];
  for (var j = 0; j < movements.length; j++) {
    var prow = moveBody.insertRow(-1);
    prow.insertCell(0).innerHTML = movements[j].nickname + ' ' + movements[j].direction;
    prow.insertCell(1).innerHTML = '$' + movements[j].amount;
    prow.insertCell(2).innerHTML = movements[j].status;
    prow.insertCell(3).innerHTML = movements[j].movement_id.slice(0, 8);
    if (movements[j].status != 'sent') {
      continue;
    }
    var moveOpt = document.createElement('OPTION');
    moveOpt.value = movements[j].movement_id;
    moveOpt.innerHTML = movements[j].nickname + ' $' + movements[j].amount;
    selection.options.add(moveOpt);
  }
}

function postStaffLinked(path, payload) {
  payload.userid = userid;
  fetch(homeURL + path, {
    method: 'post',
    body: JSON.stringify(payload),
    headers: { 'Content-type': 'application/json' }
  }).then(function(response) {
    return response.json().then(function(body) {
      var result = document.getElementById('staff_la_result');
      if (!response.ok) {
        if (result) { result.innerHTML = (body && (body.message || body.error)) || 'Failed'; }
        return;
      }
      if (result) { result.innerHTML = body.message || 'Done'; }
      getCustomer($('#customer_id_input').val() || document.getElementById('customer_id').innerHTML);
    });
  }).catch(function(error) {
    console.error(error);
  });
}

function fillStaffWires(data) {
  var snapshot = data.Wires || {};
  var card = document.getElementById('staff_wire_card');
  if (!card) {
    return;
  }
  card.style.display = 'block';
  var summary = document.getElementById('staff_wire_summary');
  if (summary) {
    summary.innerHTML = 'YTD sent: $' + (snapshot.ytd_sent || '0.00') +
      ' &middot; fees: $' + (snapshot.ytd_fees || '0.00') +
      ' &middot; recalled: $' + (snapshot.recalled_ytd || '0.00');
  }
  var beneTable = document.getElementById('staff_wire_bene_tbl');
  var beneBody = beneTable.getElementsByTagName('tbody')[0];
  beneBody.innerHTML = '';
  var benes = snapshot.beneficiaries || [];
  for (var i = 0; i < benes.length; i++) {
    var row = beneBody.insertRow(-1);
    row.insertCell(0).innerHTML = benes[i].nickname;
    row.insertCell(1).innerHTML = benes[i].legal_name;
    row.insertCell(2).innerHTML = benes[i].aba;
    row.insertCell(3).innerHTML = benes[i].status;
  }
  var table = document.getElementById('staff_wire_tbl');
  var body = table.getElementsByTagName('tbody')[0];
  body.innerHTML = '';
  var selection = document.getElementById('staff_wire_id');
  selection.options.length = 1;
  var wires = snapshot.wires || [];
  for (var j = 0; j < wires.length; j++) {
    var wrow = body.insertRow(-1);
    wrow.insertCell(0).innerHTML = wires[j].nickname;
    wrow.insertCell(1).innerHTML = '$' + wires[j].amount;
    wrow.insertCell(2).innerHTML = wires[j].status;
    wrow.insertCell(3).innerHTML = (wires[j].imad || wires[j].wire_id).slice(0, 12);
    if (['held', 'queued', 'pending_release', 'sent'].indexOf(wires[j].status) < 0) {
      continue;
    }
    var option = document.createElement('OPTION');
    option.value = wires[j].wire_id;
    option.innerHTML = wires[j].nickname + ' $' + wires[j].amount + ' (' + wires[j].status + ')';
    selection.options.add(option);
  }
}

function fillStaffInWires(data) {
  var snapshot = data.InWires || {};
  var card = document.getElementById('staff_inwire_card');
  if (!card) {
    return;
  }
  card.style.display = 'block';
  var summary = document.getElementById('staff_inwire_summary');
  if (summary) {
    summary.innerHTML = 'YTD posted: $' + (snapshot.ytd_posted || '0.00') +
      ' &middot; returned: $' + (snapshot.ytd_returned || '0.00') +
      ' &middot; open: ' + (snapshot.open_count || 0);
  }
  var table = document.getElementById('staff_inwire_tbl');
  var body = table.getElementsByTagName('tbody')[0];
  body.innerHTML = '';
  var selection = document.getElementById('staff_inwire_id');
  selection.options.length = 1;
  var rows = snapshot.inbounds || [];
  for (var i = 0; i < rows.length; i++) {
    var row = body.insertRow(-1);
    row.insertCell(0).innerHTML = rows[i].originator_name;
    row.insertCell(1).innerHTML = '$' + rows[i].amount;
    row.insertCell(2).innerHTML = rows[i].status;
    row.insertCell(3).innerHTML = (rows[i].imad || rows[i].inbound_id).slice(0, 12);
    if (['held', 'queued', 'pending_release', 'unmatched', 'posted'].indexOf(rows[i].status) < 0) {
      continue;
    }
    var option = document.createElement('OPTION');
    option.value = rows[i].inbound_id;
    option.innerHTML = rows[i].originator_name + ' $' + rows[i].amount + ' (' + rows[i].status + ')';
    selection.options.add(option);
  }
}

function postStaffInWire(path, payload) {
  payload.userid = userid;
  payload.customer_id = $('#customer_id_input').val() || document.getElementById('customer_id').innerHTML;
  fetch(homeURL + path, {
    method: 'post',
    body: JSON.stringify(payload),
    headers: { 'Content-type': 'application/json' }
  }).then(function(response) {
    return response.json().then(function(body) {
      var result = document.getElementById('staff_inwire_result');
      if (!response.ok) {
        if (result) { result.innerHTML = (body && (body.message || body.error)) || 'Failed'; }
        return;
      }
      if (result) { result.innerHTML = body.message || 'Done'; }
      getCustomer($('#customer_id_input').val() || document.getElementById('customer_id').innerHTML);
    });
  }).catch(function(error) {
    console.error(error);
  });
}

function postStaffWire(path, payload) {
  payload.userid = userid;
  fetch(homeURL + path, {
    method: 'post',
    body: JSON.stringify(payload),
    headers: { 'Content-type': 'application/json' }
  }).then(function(response) {
    return response.json().then(function(body) {
      var result = document.getElementById('staff_wire_result');
      if (!response.ok) {
        if (result) { result.innerHTML = (body && (body.message || body.error)) || 'Failed'; }
        return;
      }
      if (result) { result.innerHTML = body.message || 'Done'; }
      getCustomer($('#customer_id_input').val() || document.getElementById('customer_id').innerHTML);
    });
  }).catch(function(error) {
    console.error(error);
  });
}

function fillCustomerAccTbl(data){
  var table = document.getElementById('cust_accounts_tbl');
  var rowCount = table.rows.length;
  try {
    for(var i=1; i<rowCount; i++) {
      table.deleteRow(i);
      rowCount--;
      i--;
    }
  }catch(e) {
    alert(e);
  }

  if(data.Accounts.savings != "None"){
    var rowCount = table.rows.length;
    var row = table.insertRow(rowCount);
    var cell1 = row.insertCell(0);
	  cell1.innerHTML = data.Accounts.savings.Account;

	  var cell2 = row.insertCell(1);
	  cell2.innerHTML = 'Savings';

	  var cell3 = row.insertCell(2);
	  cell3.innerHTML = data.Accounts.savings.Balance;
  }
  if(data.Accounts.checkin != "None"){
    var rowCount = table.rows.length;
    var row = table.insertRow(rowCount);
    var cell1 = row.insertCell(0);
	  cell1.innerHTML = data.Accounts.checkin.Account;

	  var cell2 = row.insertCell(1);
	  cell2.innerHTML = 'Savings';

	  var cell3 = row.insertCell(2);
	  cell3.innerHTML = data.Accounts.checkin.Balance;
  }
  if(data.Accounts.credit != "None"){
    var rowCount = table.rows.length;
    var row = table.insertRow(rowCount);
    var cell1 = row.insertCell(0);
	  cell1.innerHTML = data.Accounts.credit.Account;

	  var cell2 = row.insertCell(1);
	  cell2.innerHTML = 'Savings';

	  var cell3 = row.insertCell(2);
	  cell3.innerHTML = data.Accounts.credit.Balance;
  }
}

function approveCustomerReq(userid, request_id) {
  console.log("approveCustomerReq called");

  const approveCustomerReqData = {
    userid : userid,
    update_req_no : request_id
  };

  fetch(homeURL+'approveUpdateInfo', {
    method : 'post',
    body : JSON.stringify(approveCustomerReqData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("approveCustomerReq response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    window.alert(data.message);
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function denyCustomerReq(userid, request_id) {
  console.log("denyCustomerReq called");

  const denyCustomerReqData = {
    userid : userid,
    update_req_no : request_id
  };

  fetch(homeURL+'denyUpdateInfo', {
    method : 'post',
    body : JSON.stringify(denyCustomerReqData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("denyCustomerReq response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    window.alert(data.message);
    getUser();
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
      window.alert('Cheque Issued!');
    }
    else {
      window.alert(data.message );
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
    if(data.message === 'done'){
      window.alert('Successfully transferred!');
    }
    else if(data.message === 'Request to be approved by Tier2 employee'){
      window.alert(data.message);
    }
    else {
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
  });
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
    if(data.message === 'Success') {
      window.alert("Amount deposited!");
    }
    else {
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
  });
}

function withdraw(userid, account, amount) {
  console.log("withdraw called");

  const withdrawData = {
    userid : userid,
    account : account,
    amount : amount
  };

  fetch(homeURL+'withdrawAmount', {
    method : 'post',
    body : JSON.stringify(withdrawData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("withdraw response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Success') {
      window.alert("Amount withdrawn!");
    }
    else {
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
  });
}

function changePassword(userid, oldPassword, newPassword) {
  const resetpwData = {
    userid : userid,
    oldPassword : oldPassword,
    newPassword : newPassword,
    requester : 'Employee',
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

    $('#logout_btn').on('click', function(){
      logout();
    });
    $('#account_details_btn').on('click', function(){
      if($('#account_details_pane').css('display')=='none'){
          $('#account_details_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#cust_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#cashier_cheques_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
    });
    $('#transactions_btn').on('click', function(){
      getUser();
      if($('#transactions_pane').css('display')=='none'){
          $('#transactions_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#cust_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#cashier_cheques_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','#FF6600');
      $('#app_dec_transactions_btn').css('background-color','maroon');
    });
    $('#app_dec_transactions_btn').on('click', function(){
      getUser();
      if($('#app_dec_transactions_pane').css('display')=='none'){
          $('#app_dec_transactions_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#cust_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#cashier_cheques_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','#FF6600');
    });
    $('#cust_requests_btn').on('click', function(){
      if($('#cust_requests_pane').css('display')=='none'){
        $('#cust_requests_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#cust_requests_btn').css('background-color','#FF6600');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#cashier_cheques_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
    });
    $('#cust_accs_btn').on('click', function(){
      getUser();
      if($('#cust_accs_pane').css('display')=='none') {
        $('#cust_accs_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','#FF6600');
      $('#cust_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#cashier_cheques_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
    });
    $('#app_dec_requests_btn').on('click', function(){
      if($('#app_dec_requests_pane').css('display')=='none') {
        $('#app_dec_requests_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#cust_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','#FF6600');
      $('#cashier_cheques_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
    });
    $('#cashier_cheques_btn').on('click', function(){
      if($('#cashier_cheques_pane').css('display')=='none') {
        $('#cashier_cheques_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#cust_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#cashier_cheques_btn').css('background-color','#FF6600');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
    });
    $('#cust_accs_btn').click();
    $(".loader-wrapper").delay( 1000 ).fadeOut("slow");
    $('#home_logo').on('click', function(){
      $('#cust_accs_btn').click();
    });
    $('#update_info_btn').on('click', function(){
      console.log("updateInfo function");
      updateInfo(userid, $('#account_email_id').val(), $('#account_contact_no').val(), $('#account_address').val());
    });
    $('#customer_id_input_btn').on('click', function(){
      if($('#customer_id_input').val() == ''){
        window.alert('No input!');
      }
      else {
        getCustomer($('#customer_id_input').val());
      }
    });
    $('#customer_id_clear_btn').on('click', function(){
      $('#cust_details_card').hide();
      $('#cust_accounts_tbl').hide();
      $('#staff_linked_card').hide();
      $('#staff_wire_card').hide();
      $('#staff_inwire_card').hide();
    });
    $('#staff_la_force_btn').on('click', function(){
      if($('#staff_la_link_id').val() == 'select'){ window.alert('Select a pending link.'); return; }
      postStaffLinked('forceVerifyLinkedAccount', { link_id: $('#staff_la_link_id').val() });
    });
    $('#staff_la_accept_btn').on('click', function(){
      if($('#staff_la_link_id').val() == 'select'){ window.alert('Select a pending link.'); return; }
      postStaffLinked('acceptPrenote', { link_id: $('#staff_la_link_id').val() });
    });
    $('#staff_la_reject_btn').on('click', function(){
      if($('#staff_la_link_id').val() == 'select'){ window.alert('Select a pending link.'); return; }
      postStaffLinked('rejectPrenote', { link_id: $('#staff_la_link_id').val() });
    });
    $('#staff_la_settle_btn').on('click', function(){
      if($('#staff_la_movement_id').val() == 'select'){ window.alert('Select a sent ACH.'); return; }
      postStaffLinked('settleLinkedAch', { movement_id: $('#staff_la_movement_id').val() });
    });
    $('#staff_la_return_btn').on('click', function(){
      if($('#staff_la_movement_id').val() == 'select'){ window.alert('Select a sent ACH.'); return; }
      postStaffLinked('returnLinkedAch', { movement_id: $('#staff_la_movement_id').val(), reason: 'unauthorized' });
    });
    $('#staff_wire_override_btn').on('click', function(){
      if($('#staff_wire_id').val() == 'select'){ window.alert('Select a wire.'); return; }
      postStaffWire('overrideOfac', { wire_id: $('#staff_wire_id').val() });
    });
    $('#staff_wire_release_btn').on('click', function(){
      if($('#staff_wire_id').val() == 'select'){ window.alert('Select a wire.'); return; }
      postStaffWire('releaseWire', { wire_id: $('#staff_wire_id').val() });
    });
    $('#staff_wire_reject_btn').on('click', function(){
      if($('#staff_wire_id').val() == 'select'){ window.alert('Select a wire.'); return; }
      postStaffWire('rejectWire', { wire_id: $('#staff_wire_id').val() });
    });
    $('#staff_wire_complete_btn').on('click', function(){
      if($('#staff_wire_id').val() == 'select'){ window.alert('Select a wire.'); return; }
      postStaffWire('completeWire', { wire_id: $('#staff_wire_id').val() });
    });
    $('#staff_wire_recall_btn').on('click', function(){
      if($('#staff_wire_id').val() == 'select'){ window.alert('Select a wire.'); return; }
      postStaffWire('recallWire', { wire_id: $('#staff_wire_id').val() });
    });
    $('#staff_inwire_override_btn').on('click', function(){
      if($('#staff_inwire_id').val() == 'select'){ window.alert('Select an inbound wire.'); return; }
      postStaffInWire('overrideInWireOfac', { inbound_id: $('#staff_inwire_id').val() });
    });
    $('#staff_inwire_release_btn').on('click', function(){
      if($('#staff_inwire_id').val() == 'select'){ window.alert('Select an inbound wire.'); return; }
      postStaffInWire('releaseInWire', { inbound_id: $('#staff_inwire_id').val() });
    });
    $('#staff_inwire_reject_btn').on('click', function(){
      if($('#staff_inwire_id').val() == 'select'){ window.alert('Select an inbound wire.'); return; }
      postStaffInWire('rejectInWire', { inbound_id: $('#staff_inwire_id').val() });
    });
    $('#staff_inwire_return_btn').on('click', function(){
      if($('#staff_inwire_id').val() == 'select'){ window.alert('Select an inbound wire.'); return; }
      postStaffInWire('returnInWire', { inbound_id: $('#staff_inwire_id').val(), reason: 'other' });
    });
    $('#staff_inwire_ingest_btn').on('click', function(){
      if($('#staff_inwire_file').val() == ''){ window.alert('Paste a FAIM file.'); return; }
      postStaffInWire('ingestInWireFile', { file: $('#staff_inwire_file').val() });
    });
    $('#approve_req_btn').on('click', function(){
      if($('#customer_req_id').val() == 'select'){
        window.alert('No input!');
      }
      else {
        approveCustomerReq(userid, $('#customer_req_id').val());
      }
    });
    $('#deny_req_btn').on('click', function(){
      if($('#customer_req_id').val() == 'select'){
        window.alert('No input!');
      }
      else {
        denyCustomerReq(userid, $('#customer_req_id').val());
      }
    });
    $('#issue_check_btn').on('click', function(){
      if($('#issue_check_from').val() == '' || $('#issue_check_to').val() == '' || $('#issue_check_amt').val() == ''){
        window.alert('Empty input!');
      }
      else {
        order_check(userid, $('#issue_check_to').val(), $('#issue_check_from').val(), $('#issue_check_amt').val());
      }
    });
    $('#deposit_check_btn').on('click', function(){
      if($('#deposit_check_no').val() == ''){
        window.alert('Empty input!');
      }
      else {
        dep_check(userid, $('#deposit_check_no').val());
      }
    });
    $('#transfer_btn').on('click', function(){
      if($('#transfer_from').val() == '' || $('#transfer_to').val() == '' || $('#transfer_amt').val() == ''){
        window.alert('Empty input!');
      }
      else {
        fund_transfer(userid, $('#transfer_from').val(), $('#transfer_to').val(), $('#transfer_amt').val());
      }
    });
    $('#deposit_btn').on('click', function(){
      if($('#deposit_to').val() == '' || $('#deposit_amt').val() == ''){
        window.alert('Empty input!');
      }
      else {
        deposit(userid, $('#deposit_to').val(), $('#deposit_amt').val());
      }
    });
    $('#withdraw_btn').on('click', function(){
      if($('#withdraw_from').val() == '' || $('#withdraw_amt').val() == ''){
        window.alert('Empty input!');
      }
      else {
        withdraw(userid, $('#withdraw_from').val(), $('#withdraw_amt').val());
      }
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
    $('#approve_trans_btn').on('click', function(){
      if($('#customer_trans_no').val() == 'select'){
        window.alert('No input!');
      }
      else {
        approve_request(userid, $('#customer_trans_no').val());
      }
    });
    $('#deny_trans_btn').on('click', function(){
      if($('#customer_trans_no').val() == 'select'){
        window.alert('No input!');
      }
      else {
        deny_request(userid, $('#customer_trans_no').val());
      }
    });
  });
